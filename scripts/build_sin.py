"""
Relogio de Carbono do SIN — pipeline de dados.

Baixa o Balanco de Energia nos Subsistemas do ONS (base horaria), calcula o
percentual renovavel e a intensidade de carbono hora a hora para os quatro
subsistemas, identifica a melhor e a pior janela de 3 horas e grava
docs/data/sin.json.

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

SUBSISTEMAS = {
    "SE": "Sudeste / Centro-Oeste",
    "S": "Sul",
    "NE": "Nordeste",
    "N": "Norte",
}
DIAS_JANELA = 45
DIAS_PERFIL_MEDIO = 30
FUSO_BR = timezone(timedelta(hours=-3))

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

FONTES = ["hidraulica", "termica", "eolica", "solar", "nuclear"]

# CMO semi-horario, estimado pelo modelo DESSEM. Base de 30 min, agregada para hora.
BASE_CMO = (
    "https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/cmo_tm/CMO_SEMIHORARIO_{ano}.parquet"
)


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


def baixar_cmo(ano: int) -> pd.DataFrame | None:
    """CMO semi-horario por subsistema, agregado para base horaria.

    Falha aqui nao derruba o build: a pagina trata a ausencia de preco.
    """
    url = BASE_CMO.format(ano=ano)
    try:
        log(f"baixando CMO {url}")
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        df = pd.read_parquet(io.BytesIO(r.content))
        log(f"  ok — {len(r.content)/1e6:.2f} MB, colunas: {list(df.columns)}")
    except Exception as exc:  # noqa: BLE001
        log(f"  AVISO — CMO indisponivel ({exc}); a pagina sai sem a aba de preco")
        return None

    df = df.rename(columns={c: c.lower() for c in df.columns})
    df["din_instante"] = pd.to_datetime(df["din_instante"], errors="coerce")
    # val_cmo vem como texto no parquet do ONS — converter explicitamente.
    df["val_cmo"] = pd.to_numeric(df["val_cmo"], errors="coerce")
    df = df.dropna(subset=["din_instante", "val_cmo"])
    df["sigla"] = df["id_subsistema"].astype(str).str.upper().str.strip()
    df["hora_cheia"] = df["din_instante"].dt.floor("h")
    horario = df.groupby(["sigla", "hora_cheia"], as_index=False)["val_cmo"].mean()
    log(f"  CMO horario: {len(horario)} linhas, ate {horario['hora_cheia'].max()}")
    return horario


def resolver_colunas(df: pd.DataFrame) -> dict[str, str | None]:
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
    log("mapeamento: " + json.dumps(mapa, ensure_ascii=False))
    faltando = [k for k, v in mapa.items() if v is None]
    if faltando:
        log(f"AVISO — sem coluna correspondente para: {faltando}")


def preparar(df: pd.DataFrame, mapa: dict[str, str | None], subsistema: str) -> pd.DataFrame:
    col_inst, col_sub = mapa["instante"], mapa["subsistema"]
    if not col_inst or not col_sub:
        raise RuntimeError("colunas de instante/subsistema nao encontradas")

    df = df.copy()
    df[col_inst] = pd.to_datetime(df[col_inst], errors="coerce")
    df = df.dropna(subset=[col_inst])
    if df[col_inst].dt.tz is not None:  # din_instante ja vem em horario de Brasilia
        df[col_inst] = df[col_inst].dt.tz_localize(None)

    df = df[df[col_sub].astype(str).str.upper().str.strip() == subsistema]
    if df.empty:
        raise RuntimeError(f"nenhuma linha para o subsistema {subsistema}")

    corte = df[col_inst].max() - pd.Timedelta(value=DIAS_JANELA, unit="D")
    df = df[df[col_inst] >= corte].sort_values(col_inst)

    for logico in FONTES + ["carga", "intercambio"]:
        col = mapa.get(logico)
        df[logico] = pd.to_numeric(df[col], errors="coerce") if col else 0.0
    df[FONTES] = df[FONTES].fillna(0.0)

    df = df.rename(columns={col_inst: "instante"})
    return df[["instante"] + FONTES + ["carga", "intercambio"]]


def intensidade_com(df: pd.DataFrame, fator_termica: float, f: dict) -> pd.Series:
    return (
        df["hidraulica"] * f["hidraulica"]
        + df["eolica"] * f["eolica"]
        + df["solar"] * f["solar"]
        + df["termica"] * fator_termica
    ) / df["total"]


def calcular(df: pd.DataFrame, fatores: dict) -> pd.DataFrame:
    f = {k: v["valor"] for k, v in fatores["fatores"].items()}
    t = fatores["fatores"]["termica"]
    df = df.copy()
    df["renovavel"] = df["hidraulica"] + df["eolica"] + df["solar"]
    df["total"] = df["renovavel"] + df["termica"] + df["nuclear"]
    df = df[df["total"] > 0]

    df["pct_renov"] = df["renovavel"] / df["total"]
    df["intensidade"] = intensidade_com(df, t["valor"], f)
    df["intensidade_min"] = intensidade_com(df, t["minimo"], f)
    df["intensidade_max"] = intensidade_com(df, t["maximo"], f)
    # Parcela que vem so da termica — comparavel ao fator do MCTI, que conta
    # apenas queima de combustivel e ignora o ciclo de vida das renovaveis.
    df["intensidade_termica"] = df["termica"] * t["valor"] / df["total"]
    return df


def janelas_3h(serie_por_hora: pd.Series) -> tuple[dict, dict]:
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


def media_janela(serie: pd.Series, inicio: int) -> float:
    return sum(float(serie.get((inicio + k) % 24, float("nan"))) for k in range(3)) / 3


def bloco_preco(dia_por_hora: pd.DataFrame, df30: pd.DataFrame, melhor: dict) -> dict | None:
    """Cruza o CMO com a intensidade: a hora mais barata e tambem a mais limpa?"""
    if "val_cmo" not in dia_por_hora or dia_por_hora["val_cmo"].notna().sum() < 24:
        return None

    s_cmo, s_int = dia_por_hora["val_cmo"], dia_por_hora["intensidade"]
    barata, cara = janelas_3h(s_cmo)
    barata = {"inicio": barata["inicio"], "fim": barata["fim"], "cmo": round(barata["intensidade"], 2)}
    cara = {"inicio": cara["inicio"], "fim": cara["fim"], "cmo": round(cara["intensidade"], 2)}

    # O que acontece com o carbono quando a decisao e tomada so pelo preco.
    barata["intensidade"] = round(media_janela(s_int, barata["inicio"]), 1)
    custo_carbono = round(barata["intensidade"] - melhor["intensidade"], 1)

    par = df30[["intensidade", "val_cmo"]].dropna() if "val_cmo" in df30 else pd.DataFrame()
    correl = spearman = horas_zero = None
    if len(par) >= 48:
        correl = round(float(par["intensidade"].corr(par["val_cmo"])), 3)
        # O CMO satura em zero quando ha excedente, o que distorce Pearson.
        # A correlacao de postos e mais honesta com essa massa de pontos no piso.
        spearman = round(float(par["intensidade"].corr(par["val_cmo"], method="spearman")), 3)
        horas_zero = int((par["val_cmo"] <= 0.01).sum())

    return {
        "janela_barata": barata,
        "janela_cara": cara,
        "cmo_na_janela_limpa": round(media_janela(s_cmo, melhor["inicio"]), 2),
        "custo_carbono_por_preco": custo_carbono,
        "coincidem": barata["inicio"] == melhor["inicio"],
        "correlacao_30d": correl,
        "correlacao_postos_30d": spearman,
        "horas_cmo_zero_30d": horas_zero,
        "n_horas_correlacao": int(len(par)),
        "horas_cmo": [
            None if pd.isna(s_cmo.get(h, float("nan"))) else round(float(s_cmo.get(h)), 2)
            for h in range(24)
        ],
    }


def bloco_subsistema(df: pd.DataFrame, sigla: str, fatores: dict, cmo: pd.DataFrame | None) -> dict:
    if cmo is not None:
        c = cmo[cmo["sigla"] == sigla][["hora_cheia", "val_cmo"]]
        df = df.merge(c, left_on="instante", right_on="hora_cheia", how="left").drop(
            columns=["hora_cheia"], errors="ignore"
        )
    ultimo = df["instante"].max()
    dia_alvo = ultimo.normalize()
    dia = df[df["instante"].dt.normalize() == dia_alvo]
    if len(dia) < 24:  # ultimo dia incompleto: usa o dia fechado anterior
        dia_alvo = dia_alvo - pd.Timedelta(value=1, unit="D")
        dia = df[df["instante"].dt.normalize() == dia_alvo]

    por_hora = dia.set_index(dia["instante"].dt.hour)
    melhor, pior = janelas_3h(por_hora["intensidade"])

    # Sensibilidade: o ganho entre janelas nos extremos da faixa da termica.
    sens = {}
    for rotulo, coluna in (("min", "intensidade_min"), ("max", "intensidade_max")):
        s = por_hora[coluna]
        sens[rotulo] = round(media_janela(s, pior["inicio"]) - media_janela(s, melhor["inicio"]), 1)

    corte30 = ultimo - pd.Timedelta(value=DIAS_PERFIL_MEDIO, unit="D")
    df30 = df[df["instante"] >= corte30]
    perfil = df30.groupby(df30["instante"].dt.hour)[["pct_renov", "intensidade"]].mean()
    preco = bloco_preco(por_hora, df30, melhor)

    def iso(ts: pd.Timestamp) -> str:
        return ts.to_pydatetime().replace(tzinfo=FUSO_BR).isoformat()

    return {
        "sigla": sigla,
        "nome": SUBSISTEMAS[sigla],
        "ultima_hora_ons": iso(ultimo),
        "dia_referencia": dia_alvo.date().isoformat(),
        "horas": [
            {
                "h": int(r.instante.hour),
                "hidr": round(r.hidraulica, 1),
                "term": round(r.termica, 1),
                "eol": round(r.eolica, 1),
                "sol": round(r.solar, 1),
                "carga": round(r.carga, 1) if pd.notna(r.carga) else None,
                "pct_renov": round(r.pct_renov, 4),
                "intensidade": round(r.intensidade, 1),
            }
            for r in dia.itertuples()
        ],
        "perfil_medio_30d": [
            {
                "hora": int(h),
                "pct_renov": round(float(l.pct_renov), 4),
                "intensidade": round(float(l.intensidade), 1),
            }
            for h, l in perfil.iterrows()
        ],
        "melhor_janela": melhor,
        "pior_janela": pior,
        "delta_kg_por_mwh": round(pior["intensidade"] - melhor["intensidade"], 1),
        "delta_sensibilidade": sens,
        "media_periodo": round(float(df["intensidade"].mean()), 1),
        "media_periodo_termica": round(float(df["intensidade_termica"].mean()), 1),
        "preco": preco,
    }


def main() -> int:
    ano = int(os.environ.get("ANO", datetime.now(FUSO_BR).year))
    fatores = json.loads(FATORES_PATH.read_text(encoding="utf-8"))

    bruto = baixar(ano)
    mapa = resolver_colunas(bruto)
    diagnostico(bruto, mapa)
    cmo = baixar_cmo(ano)

    blocos = {}
    for sigla in SUBSISTEMAS:
        df = calcular(preparar(bruto, mapa, sigla), fatores)
        blocos[sigla] = bloco_subsistema(df, sigla, fatores, cmo)
        b = blocos[sigla]
        log(
            f"{sigla}: dia {b['dia_referencia']} | melhor {b['melhor_janela']['inicio']}h "
            f"({b['melhor_janela']['intensidade']}) | pior {b['pior_janela']['inicio']}h "
            f"({b['pior_janela']['intensidade']}) | delta {b['delta_kg_por_mwh']} "
            f"(faixa {b['delta_sensibilidade']['min']}–{b['delta_sensibilidade']['max']}) | "
            f"media 45d {b['media_periodo']} (so termica {b['media_periodo_termica']})"
        )
        p = b.get("preco")
        if p:
            log(
                f"   preco: barata {p['janela_barata']['inicio']}h "
                f"(R$ {p['janela_barata']['cmo']}/MWh, {p['janela_barata']['intensidade']} kg) | "
                f"coincide com a limpa: {p['coincidem']} | "
                f"custo em carbono de decidir pelo preco: {p['custo_carbono_por_preco']} kg/MWh | "
                f"correl 30d pearson={p['correlacao_30d']} spearman={p['correlacao_postos_30d']} "
                f"(n={p['n_horas_correlacao']}, horas a custo zero={p['horas_cmo_zero_30d']})"
            )

    saida = {
        "gerado_em": datetime.now(FUSO_BR).isoformat(timespec="seconds"),
        "padrao": "SE",
        "subsistemas": blocos,
        "fatores_emissao": fatores,
        "fonte": {
            "nome": "ONS — Balanco de Energia nos Subsistemas (base horaria)",
            "url": "https://dados.ons.org.br/dataset/balanco-energia-subsistema",
            "licenca": "Creative Commons Attribution",
        },
        "fonte_preco": {
            "nome": "ONS — CMO Semi-Horario (modelo DESSEM), agregado para base horaria",
            "url": "https://dados.ons.org.br/dataset/cmo-semi-horario",
            "licenca": "Creative Commons Attribution",
            "natureza": "estimado",
        },
    }

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"gravado {SAIDA} ({SAIDA.stat().st_size/1024:.1f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
