"""Rule-based tagging: sectors, market trends, companies, sentiment.
Edit the dictionaries below to match the coverage your site needs."""
import re

SECTORS = {
    "Banking & Finance": ["bank", "nbfc", "lender", "loan", "credit", "insurance", "fintech", "mutual fund", "hdfc", "icici", "sbi", "kotak", "axis bank"],
    "IT & Technology": ["software", "it services", "tech", "semiconductor", "chip", "cloud", "ai ", "artificial intelligence", "tcs", "infosys", "wipro", "hcl"],
    "Energy & Oil": ["oil", "crude", "brent", "natural gas", "opec", "refiner", "power", "solar", "renewable", "ongc"],
    "Auto": ["auto", "automobile", "ev ", "electric vehicle", "carmaker", "maruti", "tata motors", "mahindra", "tesla"],
    "Pharma & Healthcare": ["pharma", "drug", "healthcare", "hospital", "biotech", "fda", "vaccine", "sun pharma", "cipla"],
    "FMCG & Consumer": ["fmcg", "consumer", "retail", "hul", "itc", "nestle", "britannia", "e-commerce"],
    "Metals & Mining": ["steel", "metal", "mining", "aluminium", "copper", "iron ore", "tata steel", "vedanta"],
    "Real Estate & Infra": ["real estate", "housing", "infrastructure", "cement", "construction", "realty", "l&t"],
    "Telecom & Media": ["telecom", "5g", "airtel", "jio", "spectrum", "streaming", "media"],
    "Commodities": ["gold", "silver", "commodity", "wheat", "crude", "copper"],
    "Crypto": ["bitcoin", "crypto", "ethereum", "stablecoin", "blockchain"],
}

TRENDS = {
    "Rally": ["rally", "surge", "soar", "jump", "record high", "all-time high", "gains", "climb", "bull"],
    "Selloff": ["selloff", "sell-off", "plunge", "tumble", "slump", "crash", "record low", "falls", "slide", "bear"],
    "Earnings": ["earnings", "q1", "q2", "q3", "q4", "quarterly results", "profit", "revenue", "guidance", "eps"],
    "IPO & Listings": ["ipo", "listing", "public offering", "fpo", "debut", "grey market", "gmp"],
    "M&A": ["acquisition", "acquire", "merger", "takeover", "buyout", "stake sale", "deal"],
    "Rates & Policy": ["rbi", "fed ", "federal reserve", "interest rate", "rate cut", "rate hike", "inflation", "cpi", "gdp", "budget", "sebi", "tariff", "policy"],
    "Currency": ["rupee", "dollar", "forex", "currency", "yen", "euro"],
    "FII/DII Flows": ["fii", "fpi", "dii", "foreign investors", "institutional flows"],
    "Index Moves": ["sensex", "nifty", "bank nifty", "dow", "s&p 500", "nasdaq", "ftse", "nikkei", "hang seng"],
}

from companies import COMPANIES, TICKERS   # large list lives in companies.py

BULLISH = ["rally", "surge", "jump", "gain", "record high", "beat", "upgrade", "rise", "climb", "strong"]
BEARISH = ["plunge", "tumble", "slump", "fall", "crash", "miss", "downgrade", "weak", "loss", "cut"]


def _compile(d):
    return {k: re.compile(r"(?<![a-z0-9])(?:%s)" % "|".join(re.escape(w.strip()) for w in v), re.I) for k, v in d.items()}


_S, _T = _compile(SECTORS), _compile(TRENDS)


def _compile_companies(d):
    """Aliases: plain = literal, '=XYZ' = literal case-sensitive, 're:...' = raw regex."""
    out = {}
    for name, aliases in d.items():
        ci, cs = [], []
        for a in aliases:
            if a.startswith("re:"): ci.append(a[3:])
            elif a.startswith("="): cs.append(re.escape(a[1:]))
            else: ci.append(re.escape(a))
        out[name] = (re.compile(r"(?<![\w&])(?:%s)(?![\w&])" % "|".join(ci), re.I) if ci else None,
                     re.compile(r"(?<![\w&])(?:%s)(?![\w&])" % "|".join(cs)) if cs else None)
    return out


_C = _compile_companies(COMPANIES)


def categorize(title: str, summary: str = ""):
    text = f"{title} {title} {summary}"  # title counts double
    sectors = [k for k, rx in _S.items() if rx.search(text)]
    trends = [k for k, rx in _T.items() if rx.search(text)]
    companies = [k for k, (ci, cs) in _C.items() if (ci and ci.search(text)) or (cs and cs.search(text))]
    low = title.lower()
    hit = lambda w: re.search(r"(?<![a-z])" + re.escape(w), low)   # word start: "rises" counts, "surprise" does not
    score = sum(bool(hit(w)) for w in BULLISH) - sum(bool(hit(w)) for w in BEARISH)
    sentiment = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return sectors or ["General"], trends or ["Market Update"], companies, sentiment
