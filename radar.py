#!/usr/bin/env python3
"""
RADAR DES MARCHÉS - bot de veille PEA / ETF / crypto
=====================================================
Repère ce qui "sort du lot" : volumes anormaux, hausse des discussions,
actualité récente, momentum (sans être déjà trop monté), et pénalise les
signaux de risque (petite capitalisation, pump, faible liquidité).

Sources :
  - Cours / volumes actions & ETF : Yahoo Finance (yfinance)
  - Cours / volumes crypto        : CoinGecko (API publique, clé optionnelle)
  - Actualités                    : Google Actualités (flux RSS)
  - Discussions                   : Reddit (public) et X (si token API)

Variables d'environnement (toutes optionnelles) :
  X_BEARER_TOKEN       -> active le signal X (API payante)
  COINGECKO_API_KEY    -> clé "demo" gratuite, limite moins les erreurs 429
  TELEGRAM_BOT_TOKEN   -> envoi du rapport sur Telegram (avec --send)
  TELEGRAM_CHAT_ID

Usage :
  python radar.py                 # rapport complet (actions, ETF, crypto, thèmes)
  python radar.py --no-social     # sans Reddit / X
  python radar.py --only crypto   # crypto | pea | themes
  python radar.py --send          # envoie aussi le rapport sur Telegram

Planification (cron, tous les jours ouvrés à 7h30) :
  30 7 * * 1-5 cd /chemin/radar_bot && /usr/bin/python3 radar.py --send

AVERTISSEMENT : outil de repérage, pas un conseil en investissement.
Un score élevé = attention inhabituelle, pas rendement garanti. L'éligibilité
PEA doit toujours être vérifiée auprès de l'émetteur ou de ton courtier.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import json
import math
import os
import statistics
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

# --------------------------------------------------------------------------
# CONFIGURATION (modifiable, ou surchargée par watchlist.json)
# --------------------------------------------------------------------------

# Tickers Yahoo Finance. "PEA à vérifier" : toujours confirmer l'éligibilité.
EQUITIES = {
    "AIR.PA": "Airbus", "MC.PA": "LVMH", "SU.PA": "Schneider Electric",
    "STMPA.PA": "STMicroelectronics", "ASML.AS": "ASML", "SAP.DE": "SAP",
    "SAN.PA": "Sanofi", "TTE.PA": "TotalEnergies", "RMS.PA": "Hermès",
    "DSY.PA": "Dassault Systèmes", "HO.PA": "Thales", "SAF.PA": "Safran",
    "RHM.DE": "Rheinmetall", "SIE.DE": "Siemens", "OR.PA": "L'Oréal",
    "ENR.DE": "Siemens Energy", "EXA.PA": "Exail Technologies",
    "AI.PA": "Air Liquide", "BNP.PA": "BNP Paribas", "GLE.PA": "Société Générale",
    "ACA.PA": "Crédit Agricole", "CAP.PA": "Capgemini", "EL.PA": "EssilorLuxottica",
    "KER.PA": "Kering", "LR.PA": "Legrand", "ENGI.PA": "Engie", "ORA.PA": "Orange",
    "DG.PA": "Vinci", "CS.PA": "AXA", "STLAP.PA": "Stellantis", "RNO.PA": "Renault",
    "ML.PA": "Michelin", "BN.PA": "Danone", "VIE.PA": "Veolia", "AM.PA": "Dassault Aviation",
    "ALO.PA": "Alstom", "SOI.PA": "Soitec", "ALV.DE": "Allianz", "IFX.DE": "Infineon",
    "DTE.DE": "Deutsche Telekom", "ADYEN.AS": "Adyen", "INGA.AS": "ING Groep",
}

# ETF : ticker -> (nom, indice suivi, frais annuels en % ou None = à compléter)
ETFS = {
    "CW8.PA": ("Amundi MSCI World", "MSCI World", None),
    "PANX.PA": ("Amundi Nasdaq-100", "Nasdaq-100", None),
    "PE500.PA": ("Amundi S&P 500", "S&P 500", None),
    "PAEEM.PA": ("Amundi MSCI Emerging Markets", "MSCI Emerging Markets", None),
    "ESE.PA": ("BNP Paribas S&P 500", "S&P 500", None),
    "CAC.PA": ("Amundi CAC 40", "CAC 40", None),
    "EWLD.PA": ("ETF MSCI World (EWLD)", "MSCI World", None),
}

# Réglages personnels (surchargés par watchlist.json)
SETTINGS = {"mine": [], "alert_threshold": 80}
SIGNAL_MIN = 70          # score à partir duquel un signal est enregistré dans l'historique

CORE_CRYPTOS = ["bitcoin", "ethereum", "solana"]
STABLES = {"usdt", "usdc", "dai", "usde", "fdusd", "tusd", "usds", "pyusd", "usdd"}
CRYPTO_DEEP_ANALYSIS = 14   # nb de cryptos analysées en profondeur (limites API)

THEMES = {
    "IA & semi-conducteurs": "intelligence artificielle semi-conducteurs",
    "Défense & armement": "défense armement budget militaire",
    "Nucléaire & uranium": "nucléaire uranium réacteurs",
    "Luxe": "luxe LVMH Hermès consommation",
    "Santé / obésité": "laboratoires obésité GLP-1 santé",
    "Cybersécurité": "cybersécurité cyberattaque",
    "Énergie & renouvelables": "énergies renouvelables solaire éolien",
    "Régulation crypto": "régulation crypto ETF bitcoin",
}

# Pondération du score /100 (renormalisée si une source manque)
WEIGHTS = {"volume": 30, "mentions": 25, "news": 20, "momentum": 15}
MAX_RISK_PENALTY = 10

POSITIVE = {
    "hausse", "bond", "record", "dépasse", "relève", "contrat", "accord", "rachat",
    "upgrade", "surperforme", "bénéfice", "croissance", "approbation", "partenariat",
    "surge", "beats", "soars", "rally", "record", "wins", "raises", "approval", "jumps",
}
NEGATIVE = {
    "chute", "baisse", "plonge", "avertissement", "enquête", "fraude", "perte",
    "abaisse", "downgrade", "procès", "piratage", "faillite", "dégringole",
    "plunges", "misses", "probe", "lawsuit", "hack", "bankruptcy", "falls", "cuts",
}

PARIS = ZoneInfo("Europe/Paris")
UA = {"User-Agent": "radar-bot/1.0 (veille personnelle)"}
OUT_DIR = Path(__file__).parent / "reports"
SITE_DIR = Path(__file__).parent / "docs"   # page web publiée via GitHub Pages
HISTORY_PATH = SITE_DIR / "history.json"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# MODÈLE
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    kind: str                     # "action" | "etf" | "crypto"
    symbol: str
    name: str
    note: str = ""                # ex : "PEA à vérifier", indice ETF
    price: float | None = None
    chg_1d: float | None = None   # en %
    chg_7d: float | None = None   # en %
    vol_ratio: float | None = None
    mention_growth: float | None = None
    mentions_24h: int = 0
    news_24h: int = 0
    news_growth: float | None = None
    sentiment: float | None = None
    headlines: list[str] = field(default_factory=list)
    sources: list[tuple[str, str]] = field(default_factory=list)   # (titre, lien)
    currency: str = "EUR"
    social_q: str = ""
    page_url: str = ""
    earnings_soon: str | None = None
    earnings_date: str | None = None   # résultats dans les 14 jours
    spark: list[float] = field(default_factory=list)   # cours des 30 derniers jours
    delta: float | None = None     # évolution du score vs mise à jour précédente
    mine: bool = False             # fait partie de « Mes valeurs »
    market_cap: float | None = None
    trending: bool = False
    risk_penalty: float = 0.0
    risks: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


# --------------------------------------------------------------------------
# COLLECTE : ACTUALITÉS, REDDIT, X
# --------------------------------------------------------------------------

def sentiment_of(title: str) -> float:
    words = {w.strip(".,:;!?()'\"").lower() for w in title.split()}
    pos, neg = len(words & POSITIVE), len(words & NEGATIVE)
    return (pos - neg) / max(1, pos + neg)


def news_activity(query: str) -> dict | None:
    """Articles Google Actualités sur 7 jours : volume 24h vs moyenne quotidienne."""
    url = (f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}"
           "&hl=fr&gl=FR&ceid=FR:fr")
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"  ! news '{query[:40]}': {e}")
        return None
    now = time.time()
    n24, prev, sents, heads, items = 0, 0, [], [], []
    for e in feed.entries:
        ts = getattr(e, "published_parsed", None)
        if not ts:
            continue
        age_h = (now - calendar.timegm(ts)) / 3600
        if age_h <= 24:
            n24 += 1
        else:
            prev += 1
        if age_h <= 48:
            sents.append(sentiment_of(e.title))
            if len(heads) < 3:
                heads.append(e.title)
                items.append((e.title, getattr(e, "link", "")))
    prev_avg = prev / 6
    return {
        "n24": n24,
        "growth": (n24 + 1) / (prev_avg + 1),
        "sentiment": statistics.mean(sents) if sents else None,
        "headlines": heads,
        "items": items,
    }


def reddit_activity(query: str) -> tuple[int, float] | None:
    """(posts sur 24h, moyenne quotidienne des jours précédents) - API publique."""
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "new", "t": "week", "limit": 100},
            headers=UA, timeout=15,
        )
        r.raise_for_status()
        posts = r.json()["data"]["children"]
    except Exception as e:  # noqa: BLE001
        log(f"  ! reddit '{query[:40]}': {e}")
        return None
    now = time.time()
    times = [p["data"]["created_utc"] for p in posts]
    n24 = sum(1 for t in times if now - t <= 86400)
    older = [t for t in times if now - t > 86400]
    span_days = 6.0
    if len(times) >= 100:  # résultats tronqués : on mesure la vraie fenêtre couverte
        span_days = max((now - min(times)) / 86400 - 1, 0.5)
    return n24, len(older) / span_days


def x_activity(query: str) -> tuple[int, float] | None:
    """Compteurs de tweets (API X v2 'counts/recent'), nécessite X_BEARER_TOKEN."""
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/counts/recent",
            params={"query": query, "granularity": "hour"},
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        r.raise_for_status()
        buckets = [b["tweet_count"] for b in r.json()["data"]]
    except Exception as e:  # noqa: BLE001
        log(f"  ! X '{query[:40]}': {e}")
        return None
    if len(buckets) < 48:
        return None
    n24 = sum(buckets[-24:])
    prev = buckets[:-24]
    return n24, sum(prev) / max(1, len(prev) / 24)


def attach_social(c: Candidate, reddit_q: str, x_q: str, use_social: bool) -> None:
    c.social_q = reddit_q.replace('"', "")
    if not use_social:
        return
    n24, prev, got = 0, 0.0, False
    for res in (reddit_activity(reddit_q), x_activity(x_q)):
        if res:
            got = True
            n24 += res[0]
            prev += res[1]
    time.sleep(1.0)
    if got:
        c.mentions_24h = n24
        c.mention_growth = (n24 + 1) / (prev + 1)


def attach_news(c: Candidate, query: str) -> None:
    res = news_activity(query)
    time.sleep(0.6)
    if not res:
        return
    c.news_24h = res["n24"]
    c.news_growth = res["growth"]
    c.sentiment = res["sentiment"]
    c.headlines = res["headlines"]
    c.sources = res["items"]


# --------------------------------------------------------------------------
# COLLECTE : ACTIONS & ETF (Yahoo Finance)
# --------------------------------------------------------------------------

def session_fraction(last_bar) -> float:
    """Part de la séance Euronext (9h00-17h30, Paris) écoulée si la dernière barre est celle du jour."""
    now = dt.datetime.now(PARIS)
    if now.weekday() >= 5 or last_bar.date() != now.date():
        return 1.0
    minutes = (now - now.replace(hour=9, minute=0, second=0, microsecond=0)).total_seconds() / 60
    return clip(minutes / 510, 0.1, 1.0)


def analyze_ticker(ticker: str, kind: str, name: str, note: str) -> Candidate | None:
    import yfinance as yf  # import tardif : le module crypto fonctionne sans

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="3mo", auto_adjust=True)
    except Exception as e:  # noqa: BLE001
        log(f"  ! {ticker}: {e}")
        return None
    if hist is None or len(hist) < 25:
        log(f"  ! {ticker}: historique insuffisant")
        return None
    close, vol = hist["Close"], hist["Volume"]
    c = Candidate(kind=kind, symbol=ticker, name=name, note=note,
                  page_url=f"https://finance.yahoo.com/quote/{ticker}")
    try:
        c.currency = (tk.history_metadata or {}).get("currency") or "EUR"
    except Exception:  # noqa: BLE001
        pass
    c.price = float(close.iloc[-1])
    c.spark = [round(float(v), 4) for v in close.iloc[-30:]]
    c.chg_1d = float(close.iloc[-1] / close.iloc[-2] - 1) * 100
    c.chg_7d = float(close.iloc[-1] / close.iloc[-6] - 1) * 100
    avg_vol = float(vol.iloc[-31:-1].mean())
    if avg_vol > 0:   # en cours de séance, on compare au volume attendu à cette heure
        c.vol_ratio = float(vol.iloc[-1]) / (avg_vol * session_fraction(hist.index[-1]))
    turnover = float((vol.iloc[-20:] * close.iloc[-20:]).mean())

    try:  # prochains résultats (si disponibles)
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d = dates[0]
            days = (d - dt.date.today()).days
            if 0 <= days <= 14:
                c.earnings_date = d.isoformat()
            if 0 <= days <= 7:
                c.earnings_soon = d.isoformat()
    except Exception:  # noqa: BLE001
        pass

    if turnover < 1_000_000:
        c.risks.append("liquidité faible (<1 M€/jour échangé)")
        c.risk_penalty += 5
    if abs(c.chg_1d) > 15:
        c.risks.append(f"mouvement brutal sur 1 jour ({c.chg_1d:+.0f} %)")
        c.risk_penalty += 3
    return c


def collect_pea(use_social: bool) -> list[Candidate]:
    out: list[Candidate] = []
    for ticker, name in EQUITIES.items():
        log(f"Action {ticker} ({name})")
        c = analyze_ticker(ticker, "action", name, "PEA à vérifier")
        if not c:
            continue
        attach_news(c, f'"{name}" (bourse OR action OR résultats)')
        attach_social(c, f'"{name}"', f'"{name}" -is:retweet', use_social)
        if c.vol_ratio and c.vol_ratio > 8 and c.news_24h == 0:
            c.risks.append("volume très élevé sans actualité identifiée")
            c.risk_penalty += 2
        out.append(c)
    for ticker, (name, index, ter) in ETFS.items():
        log(f"ETF {ticker} ({name})")
        fees = f"frais {ter:.2f} %" if ter is not None else "frais : voir fiche émetteur"
        c = analyze_ticker(ticker, "etf", name, f"ETF · indice {index} · {fees} · éligibilité à confirmer")
        if not c:
            continue
        attach_news(c, f'ETF "{index}" (flux OR marché OR record)')
        attach_social(c, f'"{index}" ETF', f'"{index}" ETF -is:retweet', use_social)
        out.append(c)
    return out


# --------------------------------------------------------------------------
# COLLECTE : CRYPTO (CoinGecko)
# --------------------------------------------------------------------------

CG = "https://api.coingecko.com/api/v3"


def cg_get(path: str, **params):
    headers = dict(UA)
    if os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["COINGECKO_API_KEY"]
    for attempt in range(3):
        r = requests.get(CG + path, params=params, headers=headers, timeout=20)
        if r.status_code == 429:  # limite de requêtes : on patiente
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("CoinGecko : limite de requêtes atteinte")


def collect_crypto(use_social: bool) -> list[Candidate]:
    markets = cg_get("/coins/markets", vs_currency="eur", order="market_cap_desc",
                     per_page=100, page=1, price_change_percentage="24h,7d")
    markets = [m for m in markets if m["symbol"].lower() not in STABLES and m.get("market_cap")]

    trending_ids: list[str] = []
    try:
        trending_ids = [t["item"]["id"] for t in cg_get("/search/trending").get("coins", [])]
    except Exception as e:  # noqa: BLE001
        log(f"  ! trending: {e}")
    known = {m["id"] for m in markets}
    missing = [i for i in trending_ids if i not in known]
    if missing:
        time.sleep(2)
        try:
            markets += cg_get("/coins/markets", vs_currency="eur", ids=",".join(missing),
                              price_change_percentage="24h,7d")
        except Exception as e:  # noqa: BLE001
            log(f"  ! trending markets: {e}")

    for m in markets:  # proxy d'anomalie de volume : volume 24h / capitalisation
        m["_proxy"] = (m.get("total_volume") or 0) / m["market_cap"] if m.get("market_cap") else 0
    by_proxy = sorted(markets, key=lambda m: m["_proxy"], reverse=True)
    chosen: dict[str, dict] = {}
    mine_up = {x.upper() for x in SETTINGS["mine"]}
    for m in markets:
        if m["id"] in CORE_CRYPTOS or m["id"] in trending_ids or m["symbol"].upper() in mine_up:
            chosen[m["id"]] = m
    for m in by_proxy:
        if len(chosen) >= CRYPTO_DEEP_ANALYSIS + len(CORE_CRYPTOS):
            break
        chosen.setdefault(m["id"], m)

    out: list[Candidate] = []
    for cid, m in chosen.items():
        sym, name = m["symbol"].upper(), m["name"]
        log(f"Crypto {sym} ({name})")
        c = Candidate(kind="crypto", symbol=sym, name=name,
                      note="top 100" if m.get("market_cap_rank") else "",
                      price=m.get("current_price"), market_cap=m.get("market_cap"),
                      trending=cid in trending_ids,
                      page_url=f"https://www.coingecko.com/en/coins/{cid}")
        c.chg_1d = m.get("price_change_percentage_24h_in_currency") or m.get("price_change_percentage_24h")
        c.chg_7d = m.get("price_change_percentage_7d_in_currency")

        try:  # volume 24h vs moyenne 30 jours
            time.sleep(2.5)
            chart = cg_get(f"/coins/{cid}/market_chart", vs_currency="eur", days=30, interval="daily")
            c.spark = [round(v[1], 8) for v in chart.get("prices", [])][-30:]
            vols = [v[1] for v in chart["total_volumes"]][:-1]
            avg = statistics.mean(vols[-30:]) if vols else 0
            if avg > 0 and m.get("total_volume"):
                c.vol_ratio = m["total_volume"] / avg
        except Exception as e:  # noqa: BLE001
            log(f"  ! historique volume {sym}: {e}")

        if c.market_cap and c.market_cap < 50_000_000:
            c.risks.append("petite capitalisation (<50 M€) : manipulation plus facile")
            c.risk_penalty += 4
        if m["_proxy"] > 1:
            c.risks.append("volume 24h supérieur à la capitalisation")
            c.risk_penalty += 3
        if c.chg_1d and c.chg_1d > 30:
            c.risks.append(f"hausse brutale sur 24h ({c.chg_1d:+.0f} %)")
            c.risk_penalty += 3

        attach_news(c, f'{name} crypto')
        attach_social(c, f'"{name}" crypto', f'"{name}" crypto -is:retweet', use_social)
        out.append(c)
    return out


# --------------------------------------------------------------------------
# SCORE /100
# --------------------------------------------------------------------------

def momentum_score(chg_7d: float, kind: str) -> float:
    """Récompense une hausse naissante, pas un titre déjà parti."""
    x = chg_7d / (2.5 if kind == "crypto" else 1.0)
    if x <= 0:
        return 0.0
    if x <= 10:
        return x / 10
    if x <= 25:
        return 1.0
    if x <= 40:
        return 1 - (x - 25) / 15 * 0.7
    return 0.3


def compute_score(c: Candidate) -> None:
    comps: dict[str, float] = {}
    if c.vol_ratio is not None:
        comps["volume"] = clip((c.vol_ratio - 1) / 3)                 # x1 -> 0, x4+ -> 1
        if c.vol_ratio >= 2:
            c.reasons.append(f"volume x{c.vol_ratio:.1f} vs moyenne 30 j")
    if c.mention_growth is not None:
        comps["mentions"] = clip((c.mention_growth - 1) / 2) * clip(c.mentions_24h / 5)
        if c.mention_growth >= 1.5 and c.mentions_24h >= 5:
            c.reasons.append(f"discussions x{c.mention_growth:.1f} ({c.mentions_24h} posts/24 h)")
    news = clip(c.news_24h / 5)
    if c.sentiment is not None:
        news *= 0.6 + 0.4 * (c.sentiment + 1) / 2
    if c.earnings_soon:
        news = clip(news + 0.25)
        c.reasons.append(f"résultats prévus le {c.earnings_soon}")
    comps["news"] = news
    if c.news_24h >= 3:
        c.reasons.append(f"{c.news_24h} articles en 24 h")
    if c.chg_7d is not None:
        comps["momentum"] = momentum_score(c.chg_7d, c.kind)
        limit = 60 if c.kind == "crypto" else 30
        if c.chg_7d > limit:
            c.risks.append(f"déjà très monté sur 7 j ({c.chg_7d:+.0f} %) : tendance probablement connue")
    if c.trending:
        c.reasons.append("dans les tendances CoinGecko")
        comps["news"] = clip(comps["news"] + 0.2)

    # Divergence sentiment / marché
    if c.sentiment is not None and c.chg_7d is not None:
        if c.sentiment > 0.3 and c.chg_7d < 0:
            c.reasons.append("sentiment positif mais cours en baisse (divergence)")
        elif c.sentiment < -0.3 and c.chg_7d > 5:
            c.reasons.append("cours en hausse malgré un sentiment négatif")

    wsum = sum(WEIGHTS[k] for k in comps)
    base = sum(WEIGHTS[k] * v for k, v in comps.items()) / wsum * 100 if wsum else 0
    c.components = comps
    c.score = round(clip(base - min(c.risk_penalty, MAX_RISK_PENALTY), 0, 100))


# --------------------------------------------------------------------------
# THÈMES / SECTEURS
# --------------------------------------------------------------------------

def collect_themes(use_social: bool) -> list[dict]:
    rows = []
    for label, query in THEMES.items():
        log(f"Thème {label}")
        res = news_activity(query)
        time.sleep(0.6)
        if not res:
            continue
        row = {"theme": label, "n24": res["n24"], "growth": res["growth"],
               "sentiment": res["sentiment"], "headline": res["headlines"][0] if res["headlines"] else "",
               "items": res["items"]}
        if use_social:
            rd = reddit_activity(query)
            time.sleep(1.0)
            if rd:
                row["reddit_growth"] = (rd[0] + 1) / (rd[1] + 1)
        rows.append(row)
    return sorted(rows, key=lambda r: r["growth"], reverse=True)


# --------------------------------------------------------------------------
# RAPPORT
# --------------------------------------------------------------------------

def fmt_pct(x: float | None) -> str:
    return "n/d" if x is None else f"{x:+.1f} %"


def table(cands: list[Candidate], top: int) -> list[str]:
    lines = ["| # | Valeur | Score | 1 j | 7 j | Volume | Détails |",
             "|---|--------|-------|-----|-----|--------|---------|"]
    for i, c in enumerate(sorted(cands, key=lambda c: c.score, reverse=True)[:top], 1):
        vr = f"x{c.vol_ratio:.1f}" if c.vol_ratio else "n/d"
        why = "; ".join(c.reasons) or "pas de signal marqué"
        lines.append(f"| {i} | **{c.name}** ({c.symbol}) | **{c.score}/100** | "
                     f"{fmt_pct(c.chg_1d)} | {fmt_pct(c.chg_7d)} | {vr} | {why} |")
    lines.append("")
    for c in sorted(cands, key=lambda c: c.score, reverse=True)[:top]:
        extra = []
        if c.note:
            extra.append(c.note)
        if c.risks:
            extra.append("⚠️ " + " · ".join(c.risks))
        if c.headlines:
            extra.append("📰 " + c.headlines[0])
        if extra:
            lines.append(f"- **{c.name}** : " + " | ".join(extra))
    lines.append("")
    return lines


def render_report(pea: list[Candidate], crypto: list[Candidate], themes: list[dict],
                  top: int, social: bool) -> str:
    today = dt.date.today().strftime("%d/%m/%Y")
    L = [f"# Radar des marchés - {today}", ""]
    if not social:
        L += ["_Signal réseaux sociaux désactivé : le score repose sur cours, volumes et actualités._", ""]
    elif not os.getenv("X_BEARER_TOKEN"):
        L += ["_X non connecté (pas de token) : discussions mesurées via Reddit uniquement._", ""]
    if pea:
        acts = [c for c in pea if c.kind == "action"]
        etfs = [c for c in pea if c.kind == "etf"]
        L += ["## Actions européennes (PEA à vérifier)", ""] + table(acts, top)
        L += ["## ETF (éligibilité PEA à confirmer)", ""] + table(etfs, top)
    if crypto:
        L += ["## Cryptomonnaies", ""] + table(crypto, top)
    if themes:
        L += ["## Thèmes qui accélèrent (actualité 24 h vs moyenne 6 j)", "",
              "| Thème | Articles 24 h | Accélération | Sentiment |", "|---|---|---|---|"]
        for t in themes:
            s = "n/d" if t["sentiment"] is None else f"{t['sentiment']:+.2f}"
            L.append(f"| {t['theme']} | {t['n24']} | x{t['growth']:.1f} | {s} |")
        L.append("")
    L += ["---",
          "_Outil de repérage, pas un conseil en investissement. Un score élevé signale une attention "
          "inhabituelle, pas un gain garanti ; les pics de buzz sont aussi le terrain des pumps "
          "coordonnés. Vérifie toujours l'éligibilité PEA et fais tes propres recherches._"]
    return "\n".join(L)


PAGE_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--up:#15803d;--down:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#141a22;--text:#e5e7eb;--muted:#9ca3af;--line:#243041;--up:#4ade80;--down:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:8px 0 2px}
.sub{color:var(--muted);font-size:.85rem}
nav{display:flex;gap:8px;overflow-x:auto;margin:14px 0 4px;padding-bottom:4px}
nav a{white-space:nowrap;text-decoration:none;color:var(--text);border:1px solid var(--line);background:var(--card);padding:6px 12px;border-radius:999px;font-size:.85rem}
h2{font-size:1.1rem;margin:26px 0 8px;scroll-margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.name{font-weight:600}
.sym{color:var(--muted);font-size:.8rem;font-weight:400}
.score{font-size:1.7rem;font-weight:700;line-height:1;text-align:right;white-space:nowrap}
.score small{font-size:.75rem;color:var(--muted);font-weight:500}
.bar{height:6px;background:var(--line);border-radius:99px;margin:10px 0;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:.75rem;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.chip.up{color:var(--up)}.chip.down{color:var(--down)}
.why{font-size:.88rem;margin:8px 0 0}
.risk{font-size:.82rem;color:var(--warn);margin:6px 0 0}
.news{font-size:.8rem;color:var(--muted);margin:6px 0 0}
.note{font-size:.75rem;color:var(--muted);margin:4px 0 0}
.empty{color:var(--muted);font-size:.9rem}
footer{color:var(--muted);font-size:.78rem;margin:28px 0 12px}
[hidden]{display:none!important}
.scorebox{text-align:right}
.delta{font-size:.75rem;font-weight:600;margin-top:2px}
.delta.up{color:var(--up)}.delta.down{color:var(--down)}
.filters{display:flex;gap:8px;overflow-x:auto;margin:6px 0 0;padding-bottom:4px}
.filters button{font:inherit;font-size:.82rem;white-space:nowrap;color:var(--text);background:var(--card);border:1px solid var(--line);padding:5px 12px;border-radius:99px}
.filters button.on{background:var(--text);color:var(--bg)}
.spark{display:block;width:100%;height:28px;margin:2px 0 4px}
.spark polyline{fill:none;stroke-width:1.6;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.spark.up polyline{stroke:var(--up)}.spark.down polyline{stroke:var(--down)}
.row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line);font-size:.9rem}
.price{font-size:.95rem;color:var(--muted);margin-top:2px}
.src{margin:8px 0 0;font-size:.82rem}
.src a{display:block;color:var(--text);text-decoration:none;border-left:2px solid var(--line);padding:2px 0 2px 8px;margin-bottom:4px}
.links{display:flex;gap:8px;margin:10px 0 0}
.links a{font-size:.78rem;text-decoration:none;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:3px 10px}
"""


PAGE_FILTERS = (
    '<div class="filters"><button data-f="all" class="on">Tout</button>'
    '<button data-f="action">Actions</button><button data-f="etf">ETF</button>'
    '<button data-f="crypto">Crypto</button><button data-f="hot">Score ≥ 70</button></div>')

PAGE_JS = """(function(){
if("serviceWorker"in navigator){navigator.serviceWorker.register("sw.js").catch(function(){});}
var btns=document.querySelectorAll(".filters button");
function apply(f){
  btns.forEach(function(b){b.classList.toggle("on",b.dataset.f===f);});
  document.querySelectorAll(".card[data-kind]").forEach(function(c){
    var show=f==="all"||(f==="hot"?Number(c.dataset.score)>=70:c.dataset.kind===f);
    c.hidden=!show;
  });
  document.querySelectorAll("section[data-sec]").forEach(function(s){
    if(s.querySelector(".card[data-kind]")){s.hidden=!s.querySelector(".card[data-kind]:not([hidden])");}
    else{s.hidden=f!=="all";}
  });
}
btns.forEach(function(b){b.addEventListener("click",function(){apply(b.dataset.f);});});
})();"""


def _score_color(score: float) -> str:
    return "#16a34a" if score >= 70 else "#d97706" if score >= 50 else "#6b7280"


def _chip(label: str, val: float | None) -> str:
    if val is None:
        return ""
    return f'<span class="chip {"up" if val >= 0 else "down"}">{label} {val:+.1f} %</span>'


def fmt_price(c: Candidate) -> str:
    if c.price is None:
        return ""
    dec = 2 if c.price >= 1 else 4 if c.price >= 0.01 else 6
    txt = f"{c.price:,.{dec}f}".replace(",", "\u202f").replace(".", ",")      # 1 234,56
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(c.currency, c.currency)
    return f"{txt} {sym}"


def _safe_url(url: str) -> str:
    return html.escape(url, quote=True) if url.startswith(("http://", "https://")) else ""


def _sources_html(items: list[tuple[str, str]], limit: int = 3) -> str:
    rows = [f'<a href="{u}" target="_blank" rel="noopener noreferrer">{html.escape(t)}</a>'
            for t, url in items[:limit] if (u := _safe_url(url))]
    return f'<p class="src">{"".join(rows)}</p>' if rows else ""


def _links_html(c: Candidate) -> str:
    q = quote_plus(c.social_q or c.name)
    links = [("Fiche", c.page_url)] if c.page_url else []
    links += [("Reddit", f"https://www.reddit.com/search/?q={q}&sort=new"),
              ("X", f"https://x.com/search?q={q}&f=live")]
    body = "".join(f'<a href="{_safe_url(u)}" target="_blank" rel="noopener noreferrer">{label}</a>'
                   for label, u in links)
    return f'<p class="links">{body}</p>'


def _spark_svg(vals: list[float]) -> str:
    if len(vals) < 5:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(f"{i * 100 / (n - 1):.1f},{26 - (v - lo) / span * 24:.1f}" for i, v in enumerate(vals))
    cls = "up" if vals[-1] >= vals[0] else "down"
    return (f'<svg class="spark {cls}" viewBox="0 0 100 28" preserveAspectRatio="none" role="img" '
            f'aria-label="Cours sur 30 jours"><polyline points="{pts}"/></svg>')


def _delta_html(c: Candidate) -> str:
    if c.delta is None or round(c.delta) == 0:
        return ""
    return f'<div class="delta {"up" if c.delta > 0 else "down"}">{"▲" if c.delta > 0 else "▼"} {c.delta:+.0f}</div>'


def _bilan_html(b: dict | None) -> str:
    if not b or not b.get("n_total"):
        txt = "Aucun signal enregistré pour l’instant : le suivi commence avec cette mise à jour."
    elif b["n"] < 10:
        txt = (f'{b["n_total"]} signaux (score ≥ {SIGNAL_MIN}) enregistrés, dont {b["n"]} vieux de plus de 3 jours. '
               "Trop tôt pour conclure : il faut au moins 10 signaux suivis.")
    else:
        txt = (f'Sur {b["n"]} signaux (score ≥ {SIGNAL_MIN}) suivis depuis plus de 3 jours : '
               f'variation moyenne {b["avg"]:+.1f} %, médiane {b["median"]:+.1f} %, '
               f'{b["pos"]:.0f} % en hausse. À comparer avec l’évolution du marché sur la même période.')
    return (f'<div class="card"><p class="why">{html.escape(txt)}</p>'
            '<p class="note">Un titre qui sort du radar n’est plus suivi, ce qui flatte le résultat : '
            'ce bilan indique une tendance, pas une preuve de fiabilité.</p></div>')


def _card_html(c: Candidate) -> str:
    e = html.escape
    chips = _chip("1 j", c.chg_1d) + _chip("7 j", c.chg_7d)
    if c.vol_ratio:
        chips += f'<span class="chip">volume ×{c.vol_ratio:.1f}</span>'
    if c.mention_growth is not None and c.mentions_24h >= 5:
        chips += f'<span class="chip">discussions ×{c.mention_growth:.1f}</span>'
    if c.news_24h:
        chips += f'<span class="chip">{c.news_24h} articles/24 h</span>'
    if c.earnings_date:
        chips += f'<span class="chip">résultats {dt.date.fromisoformat(c.earnings_date).strftime("%d/%m")}</span>'
    price = f'<div class="price">{e(fmt_price(c))}</div>' if c.price is not None else ""
    out = [f'<div class="card" data-kind="{c.kind}" data-score="{c.score:.0f}"><div class="top"><div><div class="name">{e(c.name)} '
           f'<span class="sym">{e(c.symbol)}</span></div>{price}</div>'
           f'<div class="scorebox"><div class="score" style="color:{_score_color(c.score)}">{c.score:.0f}<small>/100</small></div>{_delta_html(c)}</div></div>',
           f'<div class="bar"><i style="width:{c.score:.0f}%;background:{_score_color(c.score)}"></i></div>',
           _spark_svg(c.spark),
           f'<div class="chips">{chips}</div>']
    out.append(f'<p class="why">{e(" · ".join(c.reasons)) if c.reasons else "Pas de signal marqué aujourd’hui."}</p>')
    if c.risks:
        out.append(f'<p class="risk">⚠️ {e(" · ".join(c.risks))}</p>')
    out.append(_sources_html(c.sources))
    out.append(_links_html(c))
    if c.note:
        out.append(f'<p class="note">{e(c.note)}</p>')
    out.append("</div>")
    return "".join(out)


def render_html(pea: list[Candidate], crypto: list[Candidate], themes: list[dict],
                top: int, social: bool, bilan: dict | None = None) -> str:
    paris = dt.datetime.now(PARIS)
    now = paris.strftime("%d/%m/%Y à %H:%M") + " (heure de Paris)"
    weekend = ("<p class=\"note\">Week-end : les marchés actions sont fermés, les cours affichés sont ceux de la "
               "dernière séance. Les cryptos sont suivies en continu.</p>") if paris.weekday() >= 5 else ""
    sections: list[tuple[str, str, str]] = []

    def block(anchor: str, title: str, items: list[Candidate]) -> None:
        ranked = sorted([c for c in items if not c.mine], key=lambda c: c.score, reverse=True)[:top]
        body = "".join(_card_html(c) for c in ranked) or '<p class="empty">Aucune donnée aujourd’hui.</p>'
        sections.append((anchor, title, body))

    mine_items = sorted([c for c in pea + crypto if c.mine], key=lambda c: c.score, reverse=True)
    if SETTINGS["mine"]:
        sections.append(("mes-valeurs", "Mes valeurs",
                         "".join(_card_html(c) for c in mine_items)
                         or '<p class="empty">Aucune de tes valeurs n’a pu être analysée aujourd’hui.</p>'))
    if pea:
        block("actions", "Actions européennes (PEA à vérifier)", [c for c in pea if c.kind == "action"])
        block("etf", "ETF (éligibilité PEA à confirmer)", [c for c in pea if c.kind == "etf"])
    if crypto:
        block("crypto", "Cryptomonnaies", crypto)
    if themes:
        rows = []
        for t in themes:
            width = clip((t["growth"] - 1) / 3) * 100
            sent = "" if t["sentiment"] is None else f' · sentiment {t["sentiment"]:+.2f}'
            rows.append(
                f'<div class="card"><div class="top"><div class="name">{html.escape(t["theme"])}</div>'
                f'<div class="score" style="color:{_score_color(40 + width * 0.5)}">×{t["growth"]:.1f}</div></div>'
                f'<div class="bar"><i style="width:{width:.0f}%;background:{_score_color(40 + width * 0.5)}"></i></div>'
                f'<p class="note">{t["n24"]} articles en 24 h{sent}</p>'
                + _sources_html(t.get("items", []), 2) + '</div>')
        sections.append(("themes", "Thèmes qui accélèrent", "".join(rows)))

    due = sorted([c for c in pea if c.earnings_date], key=lambda c: c.earnings_date or "")
    if due:
        rows_due = "".join(f'<div class="row"><span>{dt.date.fromisoformat(c.earnings_date).strftime("%d/%m")}</span>'
                           f'<span>{html.escape(c.name)}</span></div>' for c in due)
        sections.append(("calendrier", "Résultats à venir (14 jours)",
                         f'<div class="card">{rows_due}<p class="note">Dates fournies par Yahoo Finance : '
                         'à confirmer auprès de l’entreprise.</p></div>'))
    sections.append(("bilan", "Bilan des signaux", _bilan_html(bilan)))

    nav = "".join(f'<a href="#{a}">{html.escape(t.split(" (")[0])}</a>' for a, t, _ in sections)
    body = "".join(f'<section id="{a}" data-sec="{a}"><h2>{html.escape(t)}</h2>{b}</section>'
                   for a, t, b in sections)
    if not social:
        src = "Signal réseaux sociaux désactivé."
    elif not os.getenv("X_BEARER_TOKEN"):
        src = "Discussions mesurées via Reddit uniquement (X non connecté)."
    else:
        src = "Discussions mesurées via Reddit et X."
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="Radar">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="theme-color" content="#0b1220">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        '<link rel="manifest" href="manifest.webmanifest">'
        '<link rel="apple-touch-icon" href="icon-180.png">'
        '<link rel="icon" type="image/png" href="icon-192.png">'
        '<title>Radar des marchés</title>'
        f'<style>{PAGE_CSS}</style></head><body><main>'
        f'<h1>📡 Radar des marchés</h1><div class="sub">Mis à jour le {now}</div>{weekend}'
        f'<nav>{nav}</nav>{PAGE_FILTERS}{body}'
        f'<footer>{html.escape(src)} Outil de repérage, pas un conseil en investissement : '
        'un score élevé signale une attention inhabituelle, pas un gain garanti, et les pics de buzz '
        'sont aussi le terrain des pumps coordonnés. Vérifie toujours l’éligibilité PEA.</footer>'
        f'</main><script>{PAGE_JS}</script></body></html>'
    )


SERVICE_WORKER = """const CACHE = "radar-v1";
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["./", "icon-192.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
// Réseau d'abord (toujours la version du jour), cache en secours hors connexion.
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return r;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("./")))
  );
});
"""


def _png_bytes(size: int, pixel) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def radar_icon(size: int) -> bytes:
    """Icône 'radar' dessinée sans dépendance : anneaux, balayage et points verts."""
    green = (34, 197, 94)
    half = size / 2

    def mix(base, alpha):
        return tuple(round(b + (g - b) * clip(alpha)) for b, g in zip(base, green))

    def pixel(x, y):
        dx, dy = (x + 0.5 - half) / half, (y + 0.5 - half) / half
        r = math.hypot(dx, dy)
        bg_t = clip(1 - r)
        base = (round(11 + 8 * bg_t), round(18 + 14 * bg_t), round(32 + 22 * bg_t))
        a = 0.0
        for ring in (0.28, 0.54, 0.80):                       # anneaux
            a = max(a, clip(1 - abs(r - ring) / 0.03) * 0.55)
        a = max(a, clip(1 - (abs(dx) if abs(dy) < 0.80 else 9) / 0.012) * 0.18)  # axes
        a = max(a, clip(1 - (abs(dy) if abs(dx) < 0.80 else 9) / 0.012) * 0.18)
        ang = math.degrees(math.atan2(dy, dx))                 # balayage vers le haut à droite
        if r < 0.80 and -75 <= ang <= -20:
            a = max(a, 0.10 + 0.35 * (ang + 75) / 55)
        for (bx, by, br) in ((0.38, -0.42, 0.075), (-0.32, 0.26, 0.05)):   # points détectés
            d = math.hypot(dx - bx, dy - by)
            a = max(a, clip((br - d) / 0.02 + 0.5))
            a = max(a, clip(1 - (d - br) / 0.07) * 0.35)
        return mix(base, a)

    return _png_bytes(size, pixel)


def write_pwa_files(site_dir: Path) -> None:
    """Fichiers qui font de la page une 'application' : manifeste, icônes, mode hors connexion."""
    manifest = {
        "name": "Radar des marchés", "short_name": "Radar",
        "start_url": "./", "scope": "./", "display": "standalone",
        "background_color": "#0b1220", "theme_color": "#0b1220",
        "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }
    (site_dir / "manifest.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
    (site_dir / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    custom = _custom_icons(site_dir)
    for size in (180, 192, 512):
        if not custom:
            (site_dir / f"icon-{size}.png").write_bytes(radar_icon(size))


def _custom_icons(site_dir: Path) -> bool:
    """Si icon-source.png (ou .jpg) existe à la racine du dépôt, l'utilise pour les icônes."""
    root = Path(__file__).parent
    src = next((root / n for n in ("icon-source.png", "icon-source.jpg", "icon-source.jpeg")
                if (root / n).exists()), None)
    if not src:
        return False
    try:
        from PIL import Image
        img = Image.open(src).convert("RGBA")
        side = min(img.size)                         # découpe carrée au centre
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        for size in (180, 192, 512):
            bg = Image.new("RGBA", (size, size), (11, 18, 32, 255))   # fond sombre si transparence
            bg.alpha_composite(img.resize((size, size), Image.LANCZOS))
            bg.convert("RGB").save(site_dir / f"icon-{size}.png", "PNG")
        log(f"Icône personnalisée utilisée : {src.name}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  ! icône personnalisée ignorée ({e}) : icône radar utilisée")
        return False


def build_summary(pea: list[Candidate], crypto: list[Candidate], page_url: str) -> str:
    def top3(items: list[Candidate]) -> list[str]:
        return [f"• {c.name} : {c.score:.0f}/100" for c in
                sorted(items, key=lambda c: c.score, reverse=True)[:3]]
    L = [f"📡 Radar des marchés - {dt.date.today().strftime('%d/%m/%Y')}", ""]
    for title, items in (("Actions PEA (à vérifier)", [c for c in pea if c.kind == "action"]),
                         ("ETF", [c for c in pea if c.kind == "etf"]),
                         ("Crypto", crypto)):
        if items:
            L += [title] + top3(items) + [""]
    L.append(f"Détails : {page_url}")
    L.append("Outil de repérage, pas un conseil en investissement.")
    return "\n".join(L)


def send_telegram(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram : TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants.")
        return
    for i in range(0, len(text), 4000):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[i:i + 4000]}, timeout=15)


# --------------------------------------------------------------------------
# HISTORIQUE : évolution des scores, suivi des signaux, alertes
# --------------------------------------------------------------------------

def load_history() -> dict:
    try:
        h = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        h.setdefault("last", {}); h.setdefault("signals", [])
        return h
    except Exception:  # noqa: BLE001
        return {"last": {}, "signals": []}


def update_history(hist: dict, cands: list[Candidate], now: dt.datetime) -> list[Candidate]:
    """Met à jour l'évolution des scores et le suivi des signaux. Renvoie les alertes à envoyer."""
    prev = hist.get("last", {}).get("scores", {})
    thr = SETTINGS["alert_threshold"]
    alerts = []
    for c in cands:
        if c.symbol in prev:
            c.delta = c.score - prev[c.symbol]
        if prev and c.score >= thr and prev.get(c.symbol, -1) < thr:
            alerts.append(c)               # vient de franchir le seuil (pas d'alerte à la 1re exécution)
    today = now.date().isoformat()
    price_now = {c.symbol: c.price for c in cands if c.price}
    for sig in hist["signals"]:            # prix de suivi des signaux déjà enregistrés
        if sig["symbol"] in price_now:
            sig["last_p"], sig["last_d"] = price_now[sig["symbol"]], today
    seen = {(x["symbol"], x["date"]) for x in hist["signals"]}
    for c in cands:                        # nouveaux signaux : 1 par titre et par jour
        if c.score >= SIGNAL_MIN and c.price and (c.symbol, today) not in seen:
            hist["signals"].append({"date": today, "symbol": c.symbol, "name": c.name, "kind": c.kind,
                                    "score": c.score, "p0": c.price, "last_p": c.price, "last_d": today})
    hist["signals"] = hist["signals"][-600:]
    scores = dict(prev)                    # on garde les scores des catégories absentes ce coup-ci
    scores.update({c.symbol: c.score for c in cands})
    hist["last"] = {"t": now.isoformat(timespec="minutes"), "scores": scores}
    return sorted(alerts, key=lambda c: c.score, reverse=True)


def signals_summary(hist: dict) -> dict:
    """Que sont devenus les titres à score élevé, au moins 3 jours après le signal ?"""
    rets = []
    for sig in hist.get("signals", []):
        try:
            age = (dt.date.fromisoformat(sig["last_d"]) - dt.date.fromisoformat(sig["date"])).days
        except Exception:  # noqa: BLE001
            continue
        if age >= 3 and sig.get("p0"):
            rets.append((sig["last_p"] / sig["p0"] - 1) * 100)
    out = {"n_total": len(hist.get("signals", [])), "n": len(rets)}
    if rets:
        out.update(avg=statistics.mean(rets), median=statistics.median(rets),
                   pos=100 * sum(1 for r in rets if r > 0) / len(rets))
    return out


def page_url() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    return f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""


def send_alerts(alerts: list[Candidate]) -> None:
    for c in alerts[:5]:
        d = f" ({c.delta:+.0f} depuis la dernière mise à jour)" if c.delta is not None else ""
        lines = [f"🚨 Radar : {c.name} ({c.symbol}) passe à {c.score:.0f}/100{d}"]
        if c.reasons:
            lines.append(" · ".join(c.reasons))
        if c.risks:
            lines.append("⚠️ " + " · ".join(c.risks))
        if c.sources and c.sources[0][1]:
            lines.append("📰 " + c.sources[0][1])
        if page_url():
            lines.append("Détails : " + page_url())
        lines.append("Signal d'attention, pas un conseil d'investissement.")
        send_telegram("\n".join(lines))


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def load_watchlist() -> None:
    path = Path(__file__).parent / "watchlist.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if "equities" in data:                       # remplace la liste d'origine
        EQUITIES.clear(); EQUITIES.update(data["equities"])
    if "etfs" in data:
        ETFS.clear(); ETFS.update({k: tuple(v) for k, v in data["etfs"].items()})
    EQUITIES.update(data.get("equities_add", {}))   # ajouts : {"AIR.PA": "Airbus"}
    ETFS.update({k: tuple(v) for k, v in data.get("etfs_add", {}).items()})
    for t in data.get("remove", []):                # retraits : ["EXA.PA"]
        EQUITIES.pop(t, None); ETFS.pop(t, None)
    SETTINGS["mine"] = [str(x).strip() for x in data.get("mine", []) if str(x).strip()]
    SETTINGS["alert_threshold"] = int(data.get("alert_threshold", 80))
    for t in SETTINGS["mine"]:                      # valeur perso avec suffixe boursier : on l'analyse
        if "." in t and t not in EQUITIES and t not in ETFS:
            EQUITIES[t] = t
    log(f"watchlist.json chargée ({len(EQUITIES)} actions, {len(ETFS)} ETF, {len(SETTINGS['mine'])} perso)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Radar des marchés PEA / ETF / crypto")
    ap.add_argument("--top", type=int, default=10, help="nombre de lignes par tableau")
    ap.add_argument("--no-social", action="store_true", help="désactive Reddit / X")
    ap.add_argument("--only", choices=["pea", "crypto", "themes"], help="limite à une section")
    ap.add_argument("--send", action="store_true", help="envoie le rapport sur Telegram")
    ap.add_argument("--no-alerts", action="store_true", help="désactive les alertes Telegram")
    args = ap.parse_args()

    load_watchlist()
    social = not args.no_social
    pea: list[Candidate] = []
    crypto: list[Candidate] = []
    themes: list[dict] = []

    if args.only in (None, "pea"):
        pea = collect_pea(social)
    if args.only in (None, "crypto"):
        try:
            crypto = collect_crypto(social)
        except Exception as e:  # noqa: BLE001
            log(f"Crypto indisponible : {e}")
    if args.only in (None, "themes"):
        themes = collect_themes(social)

    for c in pea + crypto:
        compute_score(c)

    mine_up = {x.upper() for x in SETTINGS["mine"]}
    for c in pea + crypto:
        c.mine = c.symbol.upper() in mine_up
    hist = load_history()
    alerts = update_history(hist, pea + crypto, dt.datetime.now(PARIS))
    bilan = signals_summary(hist)

    report = render_report(pea, crypto, themes, args.top, social)
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"radar_{dt.date.today().isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    log(f"\nRapport enregistré : {path}")

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(render_html(pea, crypto, themes, args.top, social, bilan),
                                         encoding="utf-8")
    write_pwa_files(SITE_DIR)
    HISTORY_PATH.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    log(f"Page web générée : {SITE_DIR / 'index.html'}")

    if alerts and not args.no_alerts and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        send_alerts(alerts)

    if args.send:
        repo = os.getenv("GITHUB_REPOSITORY", "")   # "pseudo/radar-bot" dans GitHub Actions
        if "/" in repo:
            owner, name = repo.split("/", 1)
            send_telegram(build_summary(pea, crypto, f"https://{owner}.github.io/{name}/"))
        else:
            send_telegram(report)


if __name__ == "__main__":
    main()
