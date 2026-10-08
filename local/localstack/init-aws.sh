#!/bin/bash
# Se ejecuta dentro del contenedor de LocalStack al terminar de arrancar (/etc/localstack/init/ready.d).
# Recrea lo que en AWS real define prisma-infra:
#   - buckets S3 y tabla DynamoDB de sesiones
#   - FLOW_MODE=sfn (por defecto): cola SQS + DLQ, Lambdas starter/invoker, máquina de estados y la notificación S3 -> SQS
#   - FLOW_MODE=legacy: Lambda `prisma-trigger` + notificación S3 -> Lambda (camino anterior, sin Step Functions)
# Debe ser idempotente: LocalStack puede reiniciarse sin recrear el contenedor.
set -uo pipefail

BUCKET_WORKFLOW="prisma-workflow"
BUCKET_SCHOOLS="prisma-schools-repos"
TABLE="chat_sessions"
REGION="us-east-1"
ACCT="000000000000"
FLOW_MODE="${FLOW_MODE:-sfn}"
WORKDIR="$(mktemp -d)"

echo "[init] buckets"
awslocal s3api create-bucket --bucket "$BUCKET_WORKFLOW" --region "$REGION" >/dev/null 2>&1 || true
awslocal s3api create-bucket --bucket "$BUCKET_SCHOOLS"  --region "$REGION" >/dev/null 2>&1 || true

echo "[init] tabla $TABLE"
awslocal dynamodb create-table \
  --table-name "$TABLE" \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
  --key-schema AttributeName=session_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" >/dev/null 2>&1 || true

# zip_lambda <archivo en /opt/lambda-src> <zip de salida>: se empaqueta como lambda_function.py
zip_lambda() {
  cp "/opt/lambda-src/$1" "$WORKDIR/lambda_function.py"
  (cd "$WORKDIR" && python3 -c "import zipfile; z=zipfile.ZipFile('$2','w',zipfile.ZIP_DEFLATED); z.write('lambda_function.py'); z.close()")
}

# crear_lambda <nombre> <zip> <variables "A=1,B=2"> <timeout>
crear_lambda() {
  if ! awslocal lambda create-function --function-name "$1" --runtime python3.12 --handler lambda_function.handler \
        --role "arn:aws:iam::$ACCT:role/lambda-role" --timeout "$4" --memory-size 128 \
        --environment "Variables={$3}" --zip-file "fileb://$WORKDIR/$2" --region "$REGION" >/dev/null 2>&1; then
    awslocal lambda update-function-code --function-name "$1" --zip-file "fileb://$WORKDIR/$2" --region "$REGION" >/dev/null 2>&1 || true
    awslocal lambda update-function-configuration --function-name "$1" --environment "Variables={$3}" --region "$REGION" >/dev/null 2>&1 || true
  fi
  awslocal lambda wait function-active-v2 --function-name "$1" --region "$REGION" 2>/dev/null || true
}

if [ "$FLOW_MODE" = "legacy" ]; then
  echo "[init] modo legacy: Lambda prisma-trigger"
  zip_lambda trigger_handler.py trigger.zip
  crear_lambda prisma-trigger trigger.zip "BACKEND_INTERNAL_URL=http://workflow:8000,INTERNAL_TOKEN=${INTERNAL_TOKEN}" 60
  awslocal s3api put-bucket-notification-configuration --bucket "$BUCKET_WORKFLOW" --region "$REGION" \
    --notification-configuration "{\"LambdaFunctionConfigurations\":[{\"Id\":\"prisma-event\",\"LambdaFunctionArn\":\"arn:aws:lambda:$REGION:$ACCT:function:prisma-trigger\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"prefix\",\"Value\":\"jobs/\"}]}}}]}"
else
  echo "[init] modo sfn: SQS + Step Functions"

  # 1) colas: principal con redrive a la DLQ tras 3 recepciones
  awslocal sqs create-queue --queue-name prisma-flujo-dlq --region "$REGION" >/dev/null 2>&1 || true
  DLQ_URL=$(awslocal sqs get-queue-url --queue-name prisma-flujo-dlq --query QueueUrl --output text --region "$REGION")
  DLQ_ARN=$(awslocal sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text --region "$REGION")
  cat > "$WORKDIR/q.json" <<JSON
{"VisibilityTimeout":"180","RedrivePolicy":"{\"deadLetterTargetArn\":\"$DLQ_ARN\",\"maxReceiveCount\":\"3\"}"}
JSON
  awslocal sqs create-queue --queue-name prisma-flujo --attributes "file://$WORKDIR/q.json" --region "$REGION" >/dev/null 2>&1 || true
  Q_URL=$(awslocal sqs get-queue-url --queue-name prisma-flujo --query QueueUrl --output text --region "$REGION")
  Q_ARN=$(awslocal sqs get-queue-attributes --queue-url "$Q_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text --region "$REGION")

  # 2) Lambda invoker (la que llama al workflow)
  zip_lambda sfn_invoker.py invoker.zip
  crear_lambda prisma-sfn-invoker invoker.zip "BACKEND_INTERNAL_URL=http://workflow:8000,INTERNAL_TOKEN=${INTERNAL_TOKEN}" 45
  INVOKER_ARN="arn:aws:lambda:$REGION:$ACCT:function:prisma-sfn-invoker"

  # 3) máquina de estados (se sustituye el único marcador de plantilla: el ARN del invoker)
  sed "s#\${InvokerArn}#$INVOKER_ARN#g" /opt/statemachine/prisma_flow.asl.json > "$WORKDIR/def.json"
  SM_ARN="arn:aws:states:$REGION:$ACCT:stateMachine:prisma-flujo"
  if awslocal stepfunctions describe-state-machine --state-machine-arn "$SM_ARN" --region "$REGION" >/dev/null 2>&1; then
    awslocal stepfunctions update-state-machine --state-machine-arn "$SM_ARN" --definition "file://$WORKDIR/def.json" --region "$REGION" >/dev/null
  else
    awslocal stepfunctions create-state-machine --name prisma-flujo --definition "file://$WORKDIR/def.json" \
      --role-arn "arn:aws:iam::$ACCT:role/sfn-role" --region "$REGION" >/dev/null
  fi

  # 4) Lambda starter + mapeo de eventos SQS -> starter (un mensaje por invocación, reporta fallos parciales)
  zip_lambda sfn_starter.py starter.zip
  crear_lambda prisma-sfn-starter starter.zip \
    "STATE_MACHINE_ARN=$SM_ARN,AWS_ENDPOINT_URL=http://localstack:4566,AWS_ACCESS_KEY_ID=test,AWS_SECRET_ACCESS_KEY=test,AWS_DEFAULT_REGION=$REGION" 30
  if [ -z "$(awslocal lambda list-event-source-mappings --function-name prisma-sfn-starter --region "$REGION" --query 'EventSourceMappings[0].UUID' --output text 2>/dev/null | grep -v None)" ]; then
    awslocal lambda create-event-source-mapping --function-name prisma-sfn-starter --event-source-arn "$Q_ARN" \
      --batch-size 1 --function-response-types ReportBatchItemFailures --region "$REGION" >/dev/null
  fi

  # 5) S3 -> SQS (REEMPLAZA la notificación hacia la Lambda: S3 no admite dos destinos para el mismo evento y prefijo)
  awslocal s3api put-bucket-notification-configuration --bucket "$BUCKET_WORKFLOW" --region "$REGION" \
    --notification-configuration "{\"QueueConfigurations\":[{\"Id\":\"prisma-flujo\",\"QueueArn\":\"$Q_ARN\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"prefix\",\"Value\":\"jobs/\"}]}}}]}"
fi

echo "[init] listo (FLOW_MODE=$FLOW_MODE)"
