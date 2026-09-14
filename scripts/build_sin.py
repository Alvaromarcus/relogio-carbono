"""
Relogio de Carbono do SIN — pipeline de dados.

Baixa o Balanco de Energia nos Subsistemas do ONS (base horaria), calcula o
percentual renovavel e a intensidade de carbono hora a hora, identifica a melhor
e a pior janela de 3 horas e grava docs/data/sin.json.

Fonte: ONS, Portal de Dados Abertos (licenca CC-BY).
https://dados.ons.org.br/dataset/balanco-energia-subsistema
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parents[1]
SAIDA = RAIZ / "docs" / "data" / "sin.json"
FATORES_PATH = Path(__file__).resolve().parent / "fatores_emissao.json"

BASE_S3 = (
    "https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/"
    "balanco_energia_subsistema_ho/BALANCO_ENERGIA_SUBSISTEMA_{ano}.{ext}"
)

SUBSISTEMAS = ["SE", "S", "NE", "N"]
DIAS_JANELA = 45          # quanto puxamos do historico
DIAS_PERFIL_MEDIO = 30    # perfil medio exibido na pagina
FUSO_BR = timezone(timedelta(hours=-3))

# Palavras-chave para resolver os nomes reais das colunas sem depender do
# esquema exato documentado. Risco no 1 do briefing.
PISTAS = {
    "instante": ["din_instante", "instante", "data"],
    "subsistema": ["id_subsistema", "cod_subsistema", "subsistema"],
    "hidraulica": ["gerhidraulica", "hidraulica", "hidr"],
    "termica": ["gertermica", "termica", "term"],
    "eolica": ["gereolica", "eolica", "eol"],
    "solar": ["gersolar", "solar"],
    "nuclear": ["gernuclear", "nuclear"],
    "carga": ["val_carga", "carga"],
    "intercambio": ["intercambio"],
}


def log(msg: str) -> None:
    print(f"[relogio] {msg}", flush=True)


def baixar(ano: int) -> pd.DataFrame:
    """Tenta parquet primeiro (menor e mais rapido); cai para CSV se falhar."""
    erros = []
    for ext in ("parquet", "csv"):
        url = BASE_S3.format(ano=ano, ext=ext)
        try:
            log(f"baixando {url}")
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            log(f"  ok — {len(r.content)/1e6:.1f} MB")
            if ext == "parquet":
                return pd.read_parquet(io.BytesIO(r.content))
            return pd.read_csv(io.BytesIO(r.content), sep=";", decimal=",")
        except Exception as exc:  # noqa: BLE001
            erros.append(f"{ext}: {exc}")
            log(f"  falhou ({exc})")
    raise RuntimeError("nao foi possivel baixar os dados do ONS: " + " | ".join(erros))


def resolver_colunas(df: pd.DataFrame) -> dict[str, str | None]:
    """Mapeia nomes logicos -> nomes reais das colunas, por palavra-chave."""
    reais = {c.lower(): c for c in df.columns}
    mapa: dict[str, str | None] = {}
    for logico, pistas in PISTAS.items():
        achado = None
        for pista in pistas:
            for baixo, original in reais.items():
                if pista in baixo:
                    achado = original
                    break
            if achado:
                break
        mapa[logico] = achado
    return mapa


def diagnostico(df: pd.DataFrame, mapa: dict[str, str | None]) -> None:
    log(f"shape={df.shape}")
    log("colunas reais: " + ", ".join(map(str, df.columns)))
    log("dtypes: " + ", ".join(f"{c}={t}" for c, t in df.dtypes.items()))
    log("mapeamento: " + json.dumps(mapa, ensure_ascii=False))
    faltando = [k for k, v in mapa.items() if v is None]
    if faltando:
        log(f"AVISO — sem coluna correspondente para: {faltando}")
    log("amostra:\n" + df.head(3).to_string())


def preparar(df: pd.DataFrame, mapa: dict[str, str | None], subsistema: str) -> pd.DataFrame:
    col_inst = mapa["instante"]
    col_sub = mapa["subsistema"]
    if not col_inst or not col_sub:
        raise RuntimeError("colunas de instante/subsistema nao encontradas")

    df = df.copy()
    df[col_inst] = pd.to_datetime(df[col_inst], errors="coerce")
    df = df.dropna(subset=[col_inst])

    # din_instante ja vem em horario de Brasilia; nao convertemos para UTC.
    if df[col_inst].dt.tz is not None:
        df[col_inst] = df[col_inst].dt.tz_localize(None)

    df = df[df[col_sub].astype(str).str.upper().str.strip() == subsistema]
    if df.empty:
        vistos = sorted(set(map(str, df[col_sub].unique()))) if col_sub in df else []
        raise RuntimeError(f"nenhuma linha para o subsistema {subsistema} (vistos: {vistos})")

    corte = df[col_inst].max() - pd.Timedelta(days=DIAS_JANELA)
    df = df[df[col_inst] >= corte].sort_values(col_inst)

    for logico in ("hidraulica", "termica", "eolica", "solar", "nuclear", "carga", "intercambio"):
        col = mapa.get(logico)
        df[logico] = pd.to_numeric(df[col], errors="coerce") if col else 0.0
    df[["hidraulica", "termica", "eolica", "solar", "nuclear"]] = df[
        ["hidraulica", "termica", "eolica", "solar", "nuclear"]
    ].fillna(0.0)

    df = df.rename(columns={col_inst: "instante"})
    return df[
        ["instante", "hidraulica", "termica", "eolica", "solar", "nuclear", "carga", "intercambio"]
    ]


def calcular(df: pd.DataFrame, fatores: dict) -> pd.DataFrame:
    f = {k: v["valor"] for k, v in fatores["fatores"].items()}
    df = df.copy()
    df["renovavel"] = df["hidraulica"] + df["eolica"] + df["solar"]
    df["total"] = df["renovavel"] + df["termica"] + df["nuclear"]
    df = df[df["total"] > 0]

    df["pct_renov"] = df["renovavel"] / df["total"]
    df["intensidade"] = (
        df["hidraulica"] * f["hidraulica"]
        + df["eolica"] * f["eolica"]
        + df["solar"] * f["solar"]
        + df["nuclear"] * f["nuclear"]
        + df["termica"] * f["termica"]
    ) / df["total"]
    return df


def janelas_3h(serie_por_hora: pd.Series) -> tuple[dict, dict]:
    """Melhor e pior janela de 3 horas consecutivas (circular no dia)."""
    horas = [float(serie_por_hora.get(h, float("nan"))) for h in range(24)]
    medias = []
    for inicio in range(24):
        trio = [horas[(inicio + k) % 24] for k in range(3)]
        if any(pd.isna(v) for v in trio):
            continue
        medias.append((inicio, sum(trio) / 3))
    if not medias:
        raise RuntimeError("sem horas suficientes para calcular janelas")
    melhor = min(medias, key=lambda t: t[1])
    pior = max(medias, key=lambda t: t[1])
    return (
        {"inicio": melhor[0], "fim": (melhor[0] + 3) % 24, "intensidade": round(melhor[1], 1)},
        {"inicio": pior[0], "fim": (pior[0] + 3) % 24, "intensidade": round(pior[1], 1)},
    )


def montar_json(df: pd.DataFrame, subsistema: str, fatores: dict) -> dict:
    ultimo = df["instante"].max()
    dia_alvo = ultimo.normalize()
    dia = df[df["instante"].dt.normalize() == dia_alvo]
    # Se o ultimo dia ainda esta incompleto, usa o dia anterior fechado para as janelas.
    if len(dia) < 20:
        dia_alvo = dia_alvo - pd.Timedelta(days=1)
        dia = df[df["instante"].dt.normalize() == dia_alvo]

    por_hora = dia.set_index(dia["instante"].dt.hour)["intensidade"]
    melhor, pior = janelas_3h(por_hora)

    corte30 = ultimo - pd.Timedelta(days=DIAS_PERFIL_MEDIO)
    perfil = (
        df[df["instante"] >= corte30]
        .groupby(df["instante"].dt.hour)[["pct_renov", "intensidade"]]
        .mean()
    )

    def iso(ts: pd.Timestamp) -> str:
        return ts.to_pydatetime().replace(tzinfo=FUSO_BR).isoformat()

    return {
        "gerado_em": datetime.now(FUSO_BR).isoformat(timespec="seconds"),
        "ultima_hora_ons": iso(ultimo),
        "dia_referencia": dia_alvo.date().isoformat(),
        "subsistema": subsistema,
        "horas": [
            {
                "h": iso(r.instante),
                "hidr": round(r.hidraulica, 1),
                "term": round(r.termica, 1),
                "eol": round(r.eolica, 1),
                "sol": round(r.solar, 1),
                "nuc": round(r.nuclear, 1),
                "carga": round(r.carga, 1) if pd.notna(r.carga) else None,
                "pct_renov": round(r.pct_renov, 4),
                "intensidade": round(r.intensidade, 1),
            }
            for r in dia.itertuples()
        ],
        "perfil_medio_30d": [
            {
                "hora": int(h),
                "pct_renov": round(float(linha.pct_renov), 4),
                "intensidade": round(float(linha.intensidade), 1),
            }
            for h, linha in perfil.iterrows()
        ],
        "melhor_janela": melhor,
        "pior_janela": pior,
        "delta_kg_por_mwh": round(pior["intensidade"] - melhor["intensidade"], 1),
        "fatores_emissao": fatores,
    }


def main() -> int:
    subsistema = os.environ.get("SUBSISTEMA", "SE").upper()
    ano = int(os.environ.get("ANO", datetime.now(FUSO_BR).year))
    fatores = json.loads(FATORES_PATH.read_text(encoding="utf-8"))

    bruto = baixar(ano)
    mapa = resolver_colunas(bruto)
    diagnostico(bruto, mapa)

    df = preparar(bruto, mapa, subsistema)
    log(f"apos filtro: {len(df)} linhas, de {df['instante'].min()} a {df['instante'].max()}")

    df = calcular(df, fatores)
    saida = montar_json(df, subsistema, fatores)

    media_periodo = float(df["intensidade"].mean())
    log(f"VALIDACAO — intensidade media do periodo: {media_periodo:.1f} kg CO2eq/MWh")
    log(f"melhor janela: {saida['melhor_janela']} | pior: {saida['pior_janela']}")
    log(f"delta: {saida['delta_kg_por_mwh']} kg CO2eq por MWh deslocado")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"gravado {SAIDA} ({SAIDA.stat().st_size/1024:.1f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
