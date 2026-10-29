"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import calendar
import json
import re
import time
import unicodedata
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


# ---------------------------------------------------------------------------------------------------------------
# TODO 1 · serie
# ---------------------------------------------------------------------------------------------------------------
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
_CACHE = {}   # (codigo, desde_api, hasta_api) -> lista de datos: no repetimos pedidos a la API


def _tipo(frecuencia):
    """Reduce la frecuencia del catálogo a 'diaria', 'mensual', 'trimestral' o 'anual'."""
    f = _sin_tildes(frecuencia)
    for tipo in ("diaria", "mensual", "trimestral", "anual"):
        if f.startswith(tipo):
            return tipo
    raise ToolError(f"Frecuencia no soportada: {frecuencia!r}.")


def _fecha_api(fecha, tipo, fin=False):
    """Convierte la fecha que da el usuario ("2025-01", "2025-01-15", "2025-T1", "2025") al formato de la API."""
    fecha = str(fecha).strip().upper()
    m = re.fullmatch(r"(\d{4})(?:-(T?)(\d{1,2}))?(?:-(\d{1,2}))?", fecha)
    if not m:
        raise ToolError(f"Fecha no válida: {fecha!r}. Usen 2025-01 (mensual), 2025-01-15 (diaria), "
                        "2025-T1 (trimestral) o 2025 (anual).")
    anio, es_trim, num, dia = int(m[1]), m[2] == "T", m[3], m[4]
    if tipo == "anual":
        return f"{anio}"
    if tipo == "trimestral":
        if num is None:
            return f"{anio}-{4 if fin else 1}"
        n = int(num)
        return f"{anio}-{n if es_trim else (n - 1) // 3 + 1}"
    mes = int(num) if num else (12 if fin else 1)
    if tipo == "mensual":
        return f"{anio}-{mes}"
    # diaria: si solo dan el mes, tomamos el primer o el último día
    if dia is None:
        dia = calendar.monthrange(anio, mes)[1] if fin else 1
    return f"{anio}-{mes:02d}-{int(dia):02d}"


def _periodo(nombre, tipo):
    """Convierte el nombre que devuelve la API ("Ene.2025", "01.Oct.26", "T1.25", "2025") a "2025-01", etc."""
    partes = nombre.strip().split(".")
    try:
        if tipo == "mensual":                                   # Ene.2025
            return f"{int(partes[1])}-{MESES[_sin_tildes(partes[0])[:3]]:02d}"
        if tipo == "diaria":                                    # 01.Oct.26
            return f"{2000 + int(partes[2]) % 100}-{MESES[_sin_tildes(partes[1])[:3]]:02d}-{int(partes[0]):02d}"
        if tipo == "trimestral":                                # T1.25
            return f"{2000 + int(partes[1]) % 100}-{partes[0].upper()}"
        return partes[0]                                        # 2025
    except (KeyError, IndexError, ValueError):
        return nombre


def _pedir(url, intentos=4):
    """Pide JSON a la API. Si responde HTML (límite de pedidos), espera y reintenta."""
    for i in range(intentos):
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            return r.json()
        except (ValueError, httpx.HTTPError):
            time.sleep(2 * (i + 1))
    raise ToolError("El BCRP no respondió con datos. Esperen unos segundos y vuelvan a intentar.")


def _datos(codigo, desde, hasta):
    ficha = _ficha(codigo)                                      # da ToolError si el código no existe
    tipo = _tipo(ficha["frecuencia"])
    d, h = _fecha_api(desde, tipo), _fecha_api(hasta, tipo, fin=True)
    url = f"{API}/{codigo}/json/{d}/{h}"
    if (codigo, d, h) not in _CACHE:
        js = _pedir(url)
        datos = []
        for p in js.get("periods", []):
            try:
                valor = float(str(p["values"][0]).replace(",", ""))
            except (ValueError, IndexError, KeyError):
                continue                                        # "n.d." y vacíos se saltan
            datos.append({"periodo": _periodo(p["name"], tipo), "valor": valor})
        _CACHE[(codigo, d, h)] = datos
    return ficha, _CACHE[(codigo, d, h)], url


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Datos oficiales de una serie del BCRP entre dos fechas, ambas incluidas.

    Úsenla cuando necesiten los valores periodo por periodo (para una sola cifra o para un gráfico).
    Fechas según la frecuencia: "2025-01" (mensual), "2025-01-15" (diaria), "2025-T1" (trimestral), "2025" (anual).
    Ejemplo: serie("PN01273PM", "2026-01", "2026-09"). Devuelve los datos y la URL de la fuente.
    """
    ficha, datos, url = _datos(codigo, desde, hasta)
    return {"codigo": ficha["codigo"], "nombre": ficha["nombre"], "frecuencia": ficha["frecuencia"],
            "datos": datos, "fuente": url}


# ---------------------------------------------------------------------------------------------------------------
# TODO 2 · resumen
# ---------------------------------------------------------------------------------------------------------------
@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Estadísticas de una serie del BCRP entre dos fechas: primer y último dato, cambio, variación %, promedio,
    máximo y mínimo (con su periodo).

    Úsenla siempre que la pregunta pida una variación, un promedio, un máximo o un mínimo: no calculen a mano.
    Mismas fechas que serie. Ejemplo: resumen("PN01273PM", "2025-12", "2026-09").
    """
    ficha, datos, url = _datos(codigo, desde, hasta)
    if not datos:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}.")
    df = pd.DataFrame(datos)
    primero, ultimo = datos[0], datos[-1]
    cambio = ultimo["valor"] - primero["valor"]
    return {
        "codigo": ficha["codigo"], "nombre": ficha["nombre"], "frecuencia": ficha["frecuencia"],
        "n": len(datos), "primero": primero, "ultimo": ultimo,
        "cambio": round(cambio, 6),
        "variacion_pct": round(cambio / primero["valor"] * 100, 4) if primero["valor"] else None,
        "promedio": round(float(df.valor.mean()), 6),
        "maximo": datos[int(df.valor.idxmax())], "minimo": datos[int(df.valor.idxmin())],
        "fuente": url,
    }


# ---------------------------------------------------------------------------------------------------------------
# TODO 3 · herramienta propia: correlacion
# ---------------------------------------------------------------------------------------------------------------
@mcp.tool(annotations=SOLO_LECTURA)
def correlacion(codigo_a: str, codigo_b: str, desde: str, hasta: str) -> dict:
    """Correlación de Pearson entre dos series del BCRP de la misma frecuencia, en los periodos que ambas tienen.

    Úsenla cuando pregunten si dos indicadores se mueven juntos (por ejemplo, el tipo de cambio y la inflación,
    o el precio del cobre y las exportaciones). Antes busquen los códigos con buscar_series.
    Ejemplo: correlacion("PN01273PM", "PN01207PM", "2015-01", "2025-12").
    Devuelve r (entre -1 y 1), el número de periodos usados, el primero y el último, y las dos fuentes.
    Correlación no es causalidad: descríbanla como asociación.
    """
    fa, da, ua = _datos(codigo_a, desde, hasta)
    fb, db, ub = _datos(codigo_b, desde, hasta)
    if _tipo(fa["frecuencia"]) != _tipo(fb["frecuencia"]):
        raise ToolError(f"{codigo_a} es {fa['frecuencia']} y {codigo_b} es {fb['frecuencia']}: "
                        "elijan dos series de la misma frecuencia.")
    df = pd.DataFrame(da).merge(pd.DataFrame(db), on="periodo", suffixes=("_a", "_b"))
    if len(df) < 3:
        raise ToolError("Hay menos de 3 periodos en común: amplíen el rango de fechas.")
    r = float(df.valor_a.corr(df.valor_b))
    fuerza = "fuerte" if abs(r) >= 0.7 else "moderada" if abs(r) >= 0.4 else "débil"
    return {"serie_a": {"codigo": fa["codigo"], "nombre": fa["nombre"]},
            "serie_b": {"codigo": fb["codigo"], "nombre": fb["nombre"]},
            "r": round(r, 4), "lectura": f"asociación {fuerza} {'positiva' if r >= 0 else 'negativa'}",
            "n": len(df), "desde": df.periodo.iloc[0], "hasta": df.periodo.iloc[-1],
            "fuentes": [ua, ub]}


# ---------------------------------------------------------------------------------------------------------------
# TODO 4 · recurso
# ---------------------------------------------------------------------------------------------------------------
@mcp.resource("bcrp://serie/{codigo}", mime_type="application/json")
def ficha_serie(codigo: str) -> str:
    """Ficha de una serie del BCRP: nombre, categoría, frecuencia, desde cuándo hay datos y última actualización."""
    return json.dumps(_ficha(codigo), ensure_ascii=False, indent=2)


SERIES_CLAVE = ["PN01273PM", "PN01271PM", "PD04722MM", "PD04638PD", "PN00027MM", "PN38784BM", "PM04923AA"]


@mcp.resource("bcrp://series/clave", mime_type="application/json")
def series_clave() -> str:
    """Las series más consultadas del BCRP (inflación, tasa de referencia, tipo de cambio, reservas, cobre, PBI)
    con su código y su ficha: un punto de partida antes de usar buscar_series."""
    return json.dumps([_ficha(c) for c in SERIES_CLAVE], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------------------------------------------
# TODO 5 · prompt
# ---------------------------------------------------------------------------------------------------------------
@mcp.prompt
def informe(tema: str) -> str:
    """Informe breve sobre un tema económico con cifras oficiales del BCRP, cada una con su código y periodo."""
    return (f"Escribe un informe de 5 viñetas sobre «{tema}» en el Perú, con datos oficiales del BCRP. "
            "Primero busca las series con buscar_series; usa resumen para variaciones, promedios y extremos, "
            "y serie para valores puntuales. No hagas cuentas a mano. En cada viñeta cita el código de la serie "
            "y el periodo, por ejemplo (PN01273PM, 2026-09). Termina con una línea que liste las URL de las fuentes.")


if __name__ == "__main__":
    mcp.run()
