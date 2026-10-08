"""Prepara en el índice de git SOLO los fragmentos de un archivo que no tocan el modelo (`flash-lite`).

Uso (desde prisma_workflow/):  python ../local/tools/stage_sin_modelo.py prisma_agents/agent.py

Motivo: agent.py tiene ediciones del dueño (cambio de modelo a 3.5) mezcladas con las nuestras y no deben commitearse.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

archivo = sys.argv[1]
diff = subprocess.run(["git", "diff", "-U0", "--", archivo], capture_output=True, text=True, encoding="utf-8").stdout
if not diff.strip():
    sys.exit(f"sin cambios en {archivo}")
cabecera, *fragmentos = re.split(r"(?m)^(?=@@ )", diff)
nuestros = [f for f in fragmentos if "flash-lite" not in f]
print(f"{archivo}: {len(fragmentos)} fragmentos, excluidos (modelo): {len(fragmentos) - len(nuestros)}, preparados: {len(nuestros)}")
if nuestros:
    fd, ruta = tempfile.mkstemp(suffix=".patch")
    os.close(fd)                       # en Windows el descriptor abierto impide borrar el archivo
    parche = Path(ruta)
    parche.write_text(cabecera + "".join(nuestros), encoding="utf-8", newline="\n")
    subprocess.run(["git", "apply", "--cached", "--unidiff-zero", str(parche)], check=True)
    parche.unlink()
