"""Figura del informe: porcentaje de aciertos, errores y «no sé» por modo, a partir de resultados.csv.

    uv add matplotlib
    uv run grafico.py          # escribe grafico.png
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

AQUI = Path(__file__).parent
res = pd.read_csv(AQUI / "resultados.csv")
orden = [m for m in ["memoria", "web", "mcp"] if m in set(res.modo)]
tabla = (res.groupby("modo").resultado.value_counts(normalize=True).unstack(fill_value=0)
         .reindex(index=orden, columns=["acierta", "se equivoca", "no sabe"], fill_value=0) * 100)
segundos = res.groupby("modo").segundos.mean().reindex(orden)

fig, ax = plt.subplots(figsize=(7, 4))
izq = pd.Series(0.0, index=orden)
for col, color in zip(tabla.columns, ["#2e7d5b", "#c0392b", "#9e9e9e"]):
    ax.barh(orden, tabla[col], left=izq, color=color, label=col)
    izq += tabla[col]
for i, m in enumerate(orden):
    ax.text(101, i, f"{segundos[m]:.1f} s", va="center", fontsize=9)
ax.set_xlim(0, 112)
ax.set_xlabel("% de las preguntas")
ax.set_title(f"Cifras del BCRP: {len(res) // len(orden)} preguntas por modo")
ax.invert_yaxis()
ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.35), ncol=3, frameon=False)
fig.tight_layout()
fig.savefig(AQUI / "grafico.png", dpi=150)
print("grafico.png listo")
