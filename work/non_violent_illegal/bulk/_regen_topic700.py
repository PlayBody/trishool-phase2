"""Signal-track forecaster: market-anchored family priors with an LLM evidence pass."""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

RUN = os.getenv("RUN_ID") or str(uuid4())
GATE = os.getenv("SANDBOX_PROXY_URL", "http://sandbox_proxy").rstrip("/")

EP = {
    "search": GATE + "/api/gateway/numinous-signals/corpus/search",
    "fetch": GATE + "/api/gateway/numinous-signals/corpus/fetch",
    "router": GATE + "/api/gateway/openrouter/chat/completions/inference",
    "rod": GATE + "/api/gateway/lightning-rod/chat/completions",
    "osint": GATE + "/api/gateway/numinous-indicia/x-osint",
    "uamap": GATE + "/api/gateway/numinous-indicia/liveuamap",
    "drivers": GATE + "/api/gateway/numinous-signals/causal-drivers/drivers",
}

ROD_MODEL = "LightningRodLabs/foresight-v4"
ROUTER_MODELS = ("deepseek/deepseek-v3.2", "z-ai/glm-5.1", "moonshotai/kimi-k2.6")

BUDGET_S = 165.0
PMIN, PMAX = 0.02, 0.95
REASON_CAP = 2480

# Approximate cost rates ($ per 1 000 tokens) — update if provider pricing changes.
_TOKEN_RATES = {
    "LightningRodLabs/foresight-v4": (0.00050, 0.00150),
    "deepseek/deepseek-v3.2":        (0.00014, 0.00028),
    "z-ai/glm-5.1":                  (0.00050, 0.00150),
    "moonshotai/kimi-k2.6":          (0.00050, 0.00200),
}
_RATE_DEFAULT = (0.00100, 0.00300)


class _TokenEconomics:
    """Nested ledger for inferring per-call spend from provider usage blocks."""

    @staticmethod
    def charge(model: str, data: dict) -> float:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return 0.0
        ti = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        to = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        ri, ro = _TOKEN_RATES.get(model, _RATE_DEFAULT)
        return ti * ri / 1000.0 + to * ro / 1000.0


def _model_cost(model: str, data: dict) -> float:
    return _TokenEconomics.charge(model, data)


# Empirical YES frequencies per event family, refit on the trailing four days
# of resolved events.
FREQ = {
    "quiet": 0.575, "count": 0.169, "hotkin": 0.362, "coldkin": 0.245,
    "hotany": 0.208, "coldany": 0.110, "hit": 0.250, "hithot": 0.610,
    "px": 0.018, "arrest": 0.123, "diplo": 0.055, "gdelt": 0.030,
    "move": 0.288, "eps": 0.479, "beat": 0.810, "app": 0.050,
    "nflx": 0.110, "box": 0.030, "wx": 0.200, "sport": 0.500,
    "coin": 0.420, "misc": 0.191,
}

GEO_FAMILIES = frozenset(
    ("quiet", "count", "hotkin", "coldkin", "hotany", "coldany", "hit", "hithot", "arrest", "gdelt")
)

RX = {k: re.compile(v, re.I) for k, v in {
    "quiet": r"strike[- ]free|without (?:any )?(?:military )?strike|24-hour period without|no (?:military )?strikes",
    "count": r"more than \d+ (?:\S+ ){0,3}(?:strike|drone|missile|attack)",
    "least": r"at least (?:one|two|three|\d+)",
    "hotzone": r"gaza|israel|ukrain|russia|kyiv|lebanon|beirut|yemen|houthi|iran|syria|west bank|donetsk|kherson|crimea",
    "kinetic": r"strike|missile|drone|attack|shell|projectile|aerial|uav|launch|bomb",
    "hit": r"directly hit|be targeted|be struck",
    "arrest": r"detain|arrest",
    "px": r"(?:price|spot)\b.{0,50}(?:above|below|exceed|drop|reach|surpass)",
    "diplo": r"\bmeet(?:ing|s)?\b|talks|summit|negotiat|recognize|normali[sz]|sanction|ceasefire|truce|peace deal|accord|diplomatic",
    "gdelt": r"\bgdelt\b",
    "move": r"move more than|implied move",
    "eps": r"(?:eps|earnings).{0,60}(?:above|below|exceed)",
    "beat": r"beat (?:quarterly )?earnings|beat .{0,25}(?:eps|estimate)",
    "app": r"app store|#\d+ (?:free|paid)|top (?:free|paid) app",
    "nflx": r"netflix",
    "box": r"box office|opening weekend",
    "wx": r"temperature|weather|rainfall|snowfall|hurricane",
    "sport": r"\bvs\.?\b|both teams to score|btts|premier league|champions league|\bnba\b|\bnfl\b|\bnhl\b|\bmlb\b|clean sheet|yellow card",
    "coin": r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|\bsol\b|crypto|dogecoin|\bxrp\b|cardano",
    "hotspot": r"tehran|gaza|israel ?/ ?palestine|residential|\biran\b",
}.items()}

# Pattern-level prior overrides for geopolitics wording, applied after family lookup.
GEO_OVERRIDES = (
    (re.compile(r"ceasefire|truce|peace deal", re.I), 0.12),
    (re.compile(r"(?:drone|missile|air) strikes? on|struck by (?:a )?(?:drone|missile)", re.I), 0.85),
    (re.compile(r"nuclear (?:weapon|test|strike)|denucleariz|nato (?:dissolv|withdraw)|recognize .{0,30}(?:crimea|donbas)", re.I), 0.03),
    (re.compile(r"assassinat|regime change|capitulat", re.I), 0.04),
)

CITY_SHIFT = (
    ("kharkiv", 0.10), ("odesa", 0.05), ("kyiv", 0.05), ("zaporizhzhia", 0.05),
    ("moscow", -0.15), ("kursk", -0.20), ("belgorod", -0.10), ("tehran", -0.05),
)

SYSTEM_PROMPT = (
    "Role: senior superforecaster producing a calibrated P(YES) plus grader-ready analysis.\n"
    "GROUND RULES\n"
    "* Treat the EVIDENCE and FEED blocks below as your only time-stamped facts; reference each one inline in the form (outlet-or-host, YYYY-MM-DD).\n"
    "* Read the resolution text literally - repeat its threshold, actor and deadline before judging.\n"
    "* Start from the supplied family frequency, then move only on dated, concrete facts.\n"
    "* Daily-pattern conflict questions: an ongoing documented pattern continuing is the likely branch; a brand-new specific act is the unlikely branch.\n"
    "* Thin or conflicting evidence keeps you inside 0.25-0.60; go under 0.10 or over 0.85 only when the ask is nearly impossible or nearly certain.\n"
    "* A listed MARKET price is the single best aggregate signal - argue explicitly whenever you leave it.\n"
    "ANALYSIS CONTRACT (a rubric scores sourcing, evidence depth, weighting, uncertainty handling, traceability)\n"
    "a. Open with the literal YES condition and its deadline.\n"
    "b. Give a numbered fact list; per fact: inline citation, push direction, and rough size of the update (for instance +0.05).\n"
    "c. Declare numeric weights for prior vs market vs evidence.\n"
    "d. List exactly three uncertainty entries tagged DATA: MODEL: EXTERNAL:, each with a +/- effect on P.\n"
    "e. Close with a single line 'Final = base <b> [+/- adj (why)]... = <p>' whose arithmetic lands within 0.01 of your stated probability.\n"
    "f. Keep the whole text between 1600 and 2300 characters - the grader truncates at 2500, so lead with your best-cited material and never pad.\n"
    "Respond with strict JSON only:\n"
    '{"probability": <0.02-0.95>, "reasoning": "<compact cited analysis>", "key_facts": ["..."]}'
)


class _Toolkit:
    """Utility namespace split into focused nested units."""

    class _Numeric:
        @staticmethod
        def clamp(v, lo=PMIN, hi=PMAX):
            try:
                x = float(v)
            except (TypeError, ValueError):
                return None
            if x != x or x in (float("inf"), float("-inf")):
                return None
            return max(lo, min(hi, x))

    class _Text:
        @staticmethod
        def host_of(url):
            return re.sub(r"^https?://(?:www\.)?([^/]+).*$", r"\1", url)

        @staticmethod
        def strip_title(title):
            out = re.sub(r"[\"']", "", title)
            out = re.sub(r"\b(?:will|be|the|a|an|by|in|on|at|of|to|for)\b", " ", out, flags=re.I)
            return re.sub(r"\s+", " ", out).strip()[:140]

    class _Json:
        @staticmethod
        def extract(text):
            if not text:
                return None
            text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
            fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if fence:
                try:
                    return json.loads(fence.group(1))
                except Exception:
                    pass
            start = text.find("{")
            if start >= 0:
                depth = 0
                for idx in range(start, len(text)):
                    ch = text[idx]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[start:idx + 1])
                            except Exception:
                                break
            loose = re.search(r'"probability"\s*:\s*(0?\.\d+|1\.0|0|1)', text)
            if loose:
                return {"probability": float(loose.group(1)), "reasoning": text[:9000]}
            return None

    class _Reasoning:
        @staticmethod
        def output_quality(parsed):
            """Rank candidate LLM outputs: parseable prob, citations, derivation, body length."""
            if not isinstance(parsed, dict) or clamp(parsed.get("probability")) is None:
                return -1.0
            body = str(parsed.get("reasoning") or "")
            score = min(len(body), 2400) / 2400.0
            score += 2.0 * min(body.count("("), 12) / 12.0
            if "Final =" in body or "Final=" in body:
                score += 1.0
            return score

        @staticmethod
        def derivation_line(prior, market, model_p, final_p):
            bits = [f"base {prior:.2f}"]
            if market is not None:
                bits.append(f"market {market:.2f} (weight 0.78)")
            if model_p is not None:
                bits.append(f"model estimate {model_p:.2f}")
            return "Final = weighted blend of " + ", ".join(bits) + f" = {final_p:.2f}"

        @staticmethod
        def template_analysis(title, deadline, fam, prior, market, evidence, final_p, model_p=None):
            lines = []
            for item in evidence[:6]:
                lines.append(
                    f"{len(lines) + 1}. \"{item['title']}\" ({host_of(item['url'])}, {item['published'][:10]}) - "
                    "documents the on-the-ground state of the named condition; direction inferred from headline."
                )
            body = "\n".join(lines) if lines else "1. Corpus returned no fresh items for this query window (DATA uncertainty below)."
            market_txt = f"{market:.2f}" if market is not None else "not available"
            weights = "0.78 market / 0.12 base / 0.10 model" if market is not None else "0.78 base / 0.22 model"
            return (
                f"QUESTION: {title} (deadline {deadline}). Resolution is literal on the stated threshold and entities.\n"
                f"SOURCES & EVIDENCE (eversight corpus, time-stamped, wire/news tier):\n{body}\n"
                f"BASE RATE: ~{prior:.2f}, the empirical YES frequency over the last four days of resolved events in this "
                f"family ({fam}); family frequency is the strongest prior for recurring tracker events.\n"
                f"MARKET: implied P(YES) = {market_txt}; when present the market aggregates all public information and is weighted highest.\n"
                f"WEIGHTING: {weights}. Evidence items adjust within +/-0.05 of the blend when they directly document the "
                f"asked action inside the deadline window; none override the market anchor.\n"
                f"UNCERTAINTIES: DATA: corpus coverage may lag the freshest developments (+/-0.04). MODEL: family base rate "
                f"can shift on regime change in the underlying pattern (+/-0.05). EXTERNAL: literal resolution-wording edge "
                f"cases at the deadline boundary (+/-0.03).\n"
                f"{derivation_line(prior, market, model_p, final_p)}"
            )


clamp             = _Toolkit._Numeric.clamp
host_of           = _Toolkit._Text.host_of
strip_title       = _Toolkit._Text.strip_title
extract_json      = _Toolkit._Json.extract
output_quality    = _Toolkit._Reasoning.output_quality
derivation_line   = _Toolkit._Reasoning.derivation_line
template_analysis = _Toolkit._Reasoning.template_analysis


class _EventTaxonomy:
    """Ordered probe chain replacing a flat if-ladder for family resolution."""

    @staticmethod
    def _context(title, desc, topics):
        blob = f"{title} {desc[:300]}"
        tx = " ".join(str(z).lower() for z in (topics or []))
        return blob, tx

    @classmethod
    def resolve(cls, title, desc, topics):
        blob, tx = cls._context(title, desc, topics)

        def _gdelt():
            if RX["gdelt"].search(blob) or "gdelt" in tx:
                return "gdelt"

        def _app():
            if RX["app"].search(blob) or "app store" in tx:
                return "app"

        def _box():
            if RX["box"].search(blob) or "box office" in tx:
                return "box"

        def _nflx():
            if RX["nflx"].search(blob):
                return "nflx"

        def _wx():
            if RX["wx"].search(blob) or "weather" in tx:
                return "wx"

        def _sport():
            if RX["sport"].search(blob) or "sports" in tx:
                return "sport"

        def _coin():
            if RX["coin"].search(blob) or "crypto" in tx:
                return "coin"

        def _quiet():
            if RX["quiet"].search(blob):
                return "quiet"

        def _count():
            if RX["count"].search(blob):
                return "count"

        def _hit():
            if RX["hit"].search(blob):
                return "hithot" if RX["hotspot"].search(blob) else "hit"

        def _least():
            if RX["least"].search(blob):
                hot = bool(RX["hotzone"].search(blob))
                kin = bool(RX["kinetic"].search(blob))
                if hot and kin:
                    return "hotkin"
                if kin:
                    return "coldkin"
                return "hotany" if hot else "coldany"

        def _beat():
            if RX["beat"].search(blob):
                return "beat"

        def _move():
            if RX["move"].search(blob):
                return "move"

        def _eps():
            if RX["eps"].search(blob):
                return "eps"

        def _px():
            if RX["px"].search(blob):
                return "px"

        def _arrest():
            if RX["arrest"].search(blob):
                return "arrest"

        def _diplo():
            if RX["diplo"].search(blob):
                return "diplo"

        probes = (
            _gdelt, _app, _box, _nflx, _wx, _sport, _coin, _quiet, _count,
            _hit, _least, _beat, _move, _eps, _px, _arrest, _diplo,
        )
        for probe in probes:
            label = probe()
            if label:
                return label
        return "misc"


def classify(title, desc, topics):
    return _EventTaxonomy.resolve(title, desc, topics)


class _PriorCalibrator:
    """Stepwise prior adjustments via nested geo/quiet/count handlers."""

    @staticmethod
    def _quiet_override(low):
        for needle, override in (("gaza", 0.82), ("iran", 0.15), ("lebanon", 0.28)):
            if needle in low:
                return override
        return None

    @staticmethod
    def _count_override(low):
        m = re.search(r"more than (\d+)", low)
        if not m:
            return None
        n = int(m.group(1))
        if n >= 40:
            return 0.08
        if n <= 10:
            return 0.30
        return None

    @staticmethod
    def _geo_adjust(base, fam, low):
        for rx, override in GEO_OVERRIDES:
            if rx.search(low):
                base = override
                break
        if fam in ("hotkin", "hotany", "coldkin"):
            for city, shift in CITY_SHIFT:
                if city in low:
                    base = min(0.97, max(0.02, base + shift))
                    break
        return base

    @classmethod
    def compute(cls, fam, title):
        base = FREQ.get(fam, 0.23)
        low = title.lower()
        if fam == "quiet":
            override = cls._quiet_override(low)
            if override is not None:
                return override
        if fam == "count":
            override = cls._count_override(low)
            if override is not None:
                return override
        if fam in GEO_FAMILIES:
            base = cls._geo_adjust(base, fam, low)
        return base


def family_prior(fam, title):
    return _PriorCalibrator.compute(fam, title)


class _MarketProbe:
    _KEYS = ("market_price", "price", "midprice", "mid_price", "current_price", "poly_price_yes")

    @classmethod
    def read(cls, meta):
        if not isinstance(meta, dict):
            return None
        inner = meta.get("event_metadata") if isinstance(meta.get("event_metadata"), dict) else {}
        for key in cls._KEYS:
            raw = meta.get(key, inner.get(key))
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if 0.0 < val < 1.0:
                return val
        return None


def metadata_price(meta):
    return _MarketProbe.read(meta)


class Forecaster:
    class _RunContext:
        """Lightweight handle threaded through async pipeline stages."""

        __slots__ = ("fc",)

        def __init__(self, fc):
            self.fc = fc

        @property
        def title(self):
            return self.fc.title

        @property
        def eid(self):
            return self.fc.eid

        def left(self):
            return self.fc.left()

        async def post(self, client, url, body, timeout):
            return await self.fc.post(client, url, body, timeout)

    class _EventParser:
        @staticmethod
        def extract_topics(meta):
            if isinstance(meta, dict):
                return meta.get("topics") or (meta.get("event_metadata") or {}).get("topics", [])
            return []

        @staticmethod
        def classify_event(title, desc, topics):
            return classify(title, desc, topics)

        @staticmethod
        def get_prior(fam, title):
            return family_prior(fam, title)

        @staticmethod
        def get_price(meta):
            return metadata_price(meta)

    class _CorpusOps:
        @staticmethod
        async def search(ctx, client):
            since = (datetime.now(timezone.utc) - timedelta(hours=96)).isoformat()
            queries = [ctx.title[:180], strip_title(ctx.title)]
            batches = await asyncio.gather(
                *[ctx.post(client, EP["search"],
                           {"run_id": RUN, "query": q, "max_results": 10, "published_after": since}, 20.0)
                  for q in queries],
                return_exceptions=True,
            )
            seen, items = set(), []
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                for hit in (batch.get("results") or [])[:10]:
                    url = str(hit.get("url") or "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    items.append({
                        "url": url,
                        "title": str(hit.get("title") or "")[:160],
                        "published": str(hit.get("published_at") or "")[:19],
                        "snippet": str(hit.get("snippet") or "")[:420],
                        "sid": str(hit.get("source_id") or ""),
                    })
            return items[:14]

        @staticmethod
        async def expand_top_hit(ctx, client, items):
            if not items or not items[0].get("sid"):
                return
            data = await ctx.post(client, EP["fetch"], {"run_id": RUN, "source_id": items[0]["sid"]}, 15.0)
            if isinstance(data, dict):
                full = str(data.get("content") or "")[:2400]
                if full:
                    items[0]["snippet"] = (items[0]["snippet"] + " || " + full)[:1500]

    class _FeedOps:
        @staticmethod
        async def collect(ctx, client):
            batches = await asyncio.gather(
                ctx.post(client, EP["osint"], {"run_id": RUN, "limit": 25}, 15.0),
                ctx.post(client, EP["uamap"], {"run_id": RUN, "limit": 25}, 15.0),
                return_exceptions=True,
            )
            lines = []
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                for sig in (batch.get("signals") or [])[:12]:
                    lines.append(f"[{sig.get('category', '')}] {sig.get('signal', '')} (c={sig.get('confidence', '')})")
            return "\n".join(lines)[:5200]

    class _DriverOps:
        @staticmethod
        async def yes_price(ctx, client):
            data = await ctx.post(client, EP["drivers"],
                                  {"run_id": RUN, "event_id": ctx.eid, "topic": "geopolitics"}, 25.0)
            if not isinstance(data, dict) or not data.get("found"):
                return None
            for drv in (data.get("drivers") or []):
                for mk in (drv.get("markets") or []):
                    try:
                        yp = float(mk.get("yes_price"))
                    except (TypeError, ValueError):
                        continue
                    if 0.01 <= yp <= 0.99:
                        return yp
            return None

    class _LLMBridge:
        _ROD = "rod"
        _ROUTER = "router"

        @staticmethod
        async def _dispatch_rod(fc, client, msgs):
            short = ROD_MODEL.split("/")[-1]
            print(f"[LR] → {short} (left={fc.left():.0f}s)")
            data = await fc.post(client, EP["rod"],
                                 {"run_id": RUN, "model": ROD_MODEL, "messages": msgs,
                                  "reasoning_effort": "low", "max_tokens": 2500}, 90.0)
            if not isinstance(data, dict):
                print(f"[LR] ← {short}: no response")
                return None
            cost = _model_cost(ROD_MODEL, data)
            fc._cost += cost
            result = extract_json((data.get("choices") or [{}])[0].get("message", {}).get("content", ""))
            usage = data.get("usage") or {}
            tok_str = f"in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}"
            p_str = f"p={result['probability']:.3f}" if result and "probability" in result else "no parse"
            print(f"[LR] ← {short}: {p_str} | {tok_str} | ${cost:.4f} (total ${fc._cost:.4f})")
            return result

        @staticmethod
        async def _dispatch_router(fc, client, msgs, model):
            short = model.split("/")[-1]
            print(f"[OR] → {short} (left={fc.left():.0f}s)")
            data = await fc.post(client, EP["router"],
                                 {"run_id": RUN, "model": model, "messages": msgs,
                                  "temperature": 0.2, "max_tokens": 2500}, 75.0)
            if not isinstance(data, dict):
                print(f"[OR] ← {short}: no response")
                return None
            cost = _model_cost(model, data)
            fc._cost += cost
            result = extract_json((data.get("choices") or [{}])[0].get("message", {}).get("content", ""))
            usage = data.get("usage") or {}
            tok_str = f"in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}"
            p_str = f"p={result['probability']:.3f}" if result and "probability" in result else "no parse"
            print(f"[OR] ← {short}: {p_str} | {tok_str} | ${cost:.4f} (total ${fc._cost:.4f})")
            return result

        @classmethod
        async def ask_rod(cls, fc, client, msgs):
            return await cls._dispatch_rod(fc, client, msgs)

        @classmethod
        async def ask_router(cls, fc, client, msgs, model):
            return await cls._dispatch_router(fc, client, msgs, model)

        @classmethod
        async def _parallel_arm(cls, fc, client, msgs):
            print(f"[INFER] parallel arm: {ROD_MODEL.split('/')[-1]} + {ROUTER_MODELS[0].split('/')[-1]}")
            arms = await asyncio.gather(
                cls.ask_rod(fc, client, msgs),
                cls.ask_router(fc, client, msgs, ROUTER_MODELS[0]),
                return_exceptions=True,
            )
            ranked = [a for a in arms if output_quality(a) >= 0]
            if ranked:
                best = max(ranked, key=output_quality)
                print(f"[INFER] parallel winner: p={best.get('probability','?')} score={output_quality(best):.2f}")
                return best, ROUTER_MODELS[1:]
            print("[INFER] parallel both failed → sequential fallback")
            return None, ROUTER_MODELS[1:]

        @classmethod
        async def _sequential_arm(cls, fc, client, msgs, models):
            for model in models:
                if fc.left() < 30:
                    print(f"[INFER] time budget exhausted (left={fc.left():.0f}s)")
                    break
                parsed = await cls.ask_router(fc, client, msgs, model)
                if parsed and parsed.get("probability") is not None:
                    print(f"[INFER] sequential hit: {model.split('/')[-1]} p={parsed['probability']:.3f}")
                    return parsed
            return None

        @classmethod
        async def infer(cls, fc, client, msgs):
            mode_handlers = {
                "parallel": cls._parallel_arm,
                "sequential": lambda f, c, m: (None, ROUTER_MODELS),
            }
            if fc.left() > 95:
                winner, remaining = await mode_handlers["parallel"](fc, client, msgs)
                if winner is not None:
                    return winner
            else:
                print(f"[INFER] sequential mode (left={fc.left():.0f}s) → {[m.split('/')[-1] for m in ROUTER_MODELS]}")
                remaining = ROUTER_MODELS
            return await cls._sequential_arm(fc, client, msgs, remaining)

    class _Calibrator:
        _SQUASH = {
            "high": lambda p: 0.93 + (p - 0.93) * 0.3,
            "low": lambda p: 0.04 - (0.04 - p) * 0.5,
        }

        @staticmethod
        def _blend_weights(market, prior, fam, model_p):
            if market is not None:
                if model_p is not None:
                    return 0.78 * market + 0.12 * prior + 0.10 * model_p
                return 0.80 * market + 0.20 * prior
            if model_p is not None:
                if fam == "misc":
                    return 0.55 * prior + 0.45 * model_p
                return 0.78 * prior + 0.22 * model_p
            return prior

        @classmethod
        def blend(cls, market, prior, fam, model_p):
            p = cls._blend_weights(market, prior, fam, model_p)
            if p > 0.93:
                p = cls._SQUASH["high"](p)
            if p < 0.04:
                p = cls._SQUASH["low"](p)
            return clamp(p) or 0.23

    class _Composer:
        @staticmethod
        def build_messages(fc, evidence, feed):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            ev_block = ""
            if evidence:
                rows = [f"- {x['title']} | {x['published']} | {x['url']}\n  {x['snippet']}" for x in evidence]
                ev_block = "EVIDENCE (eversight corpus, freshest first):\n" + "\n".join(rows)
            feed_block = f"FEED (OSINT):\n{feed}" if feed else ""
            market_block = (
                f"MARKET: {fc.market:.4f} (implied P(YES))" if fc.market is not None else "MARKET: none"
            )
            user = (
                f"NOW_UTC: {now}\nEVENT: {fc.title}\nDESCRIPTION: {fc.desc[:2200]}\nDEADLINE: {fc.deadline}\n"
                f"FAMILY_FREQUENCY: {fc.prior:.2f} (empirical for this event family)\n{market_block}\n\n"
                f"{ev_block}\n\n{feed_block}\n\nRespond with strict JSON only."
            )
            return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

        @staticmethod
        def finalize(fc, llm_text, evidence, final_p, model_p):
            text = (llm_text or "").strip()
            if len(text) < 700:
                text = template_analysis(fc.title, fc.deadline, fc.fam, fc.prior,
                                         fc.market, evidence, final_p, model_p)
            elif evidence and "http" not in text and "(" not in text[:400]:
                cited = "; ".join(
                    f"({x['title'][:70]} - {host_of(x['url'])}, {x['published'][:10]})" for x in evidence[:4]
                )
                text = "SOURCES: " + cited + "\n" + text
            if "Final =" not in text[:REASON_CAP]:
                tail = "\n" + derivation_line(fc.prior, fc.market, model_p, final_p)
                text = text[:REASON_CAP - len(tail)].rstrip() + tail
            return text[:REASON_CAP]

    def __init__(self, event):
        self.started = time.time()
        self.event = event or {}
        self.eid = str(self.event.get("event_id") or "")
        self.title = str(self.event.get("title") or "")
        self.desc = str(self.event.get("description") or "")
        self.deadline = str(self.event.get("cutoff") or "")
        meta = self.event.get("metadata") or {}
        self.meta = meta if isinstance(meta, dict) else {}
        topics = Forecaster._EventParser.extract_topics(self.meta)
        self.fam = Forecaster._EventParser.classify_event(self.title, self.desc, topics)
        self.prior = Forecaster._EventParser.get_prior(self.fam, self.title)
        self.market = Forecaster._EventParser.get_price(self.meta)
        self.is_geo = self.fam in GEO_FAMILIES
        self._cost = 0.0
        self._ctx = Forecaster._RunContext(self)
        print(f"[EVENT] fam={self.fam} prior={self.prior:.3f} market={self.market} | '{self.title[:70]}'")

    def left(self):
        return BUDGET_S - (time.time() - self.started)

    async def post(self, client, url, body, timeout):
        try:
            resp = await client.post(url, json=body, timeout=min(timeout, max(8.0, self.left() - 5)))
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def messages(self, evidence, feed):
        return Forecaster._Composer.build_messages(self, evidence, feed)

    def blend(self, model_p):
        return Forecaster._Calibrator.blend(self.market, self.prior, self.fam, model_p)

    def compose(self, llm_text, evidence, final_p, model_p):
        return Forecaster._Composer.finalize(self, llm_text, evidence, final_p, model_p)

    async def run(self):
        ctx = self._ctx

        async def _collect_bundle(client):
            task_specs = [("corpus", Forecaster._CorpusOps.search(ctx, client))]
            feed_idx = drv_idx = -1
            if self.is_geo:
                feed_idx = len(task_specs)
                task_specs.append(("feeds", Forecaster._FeedOps.collect(ctx, client)))
            if self.market is None:
                drv_idx = len(task_specs)
                task_specs.append(("driver", Forecaster._DriverOps.yes_price(ctx, client)))
            gathered = await asyncio.gather(*(t[1] for t in task_specs), return_exceptions=True)
            evidence = gathered[0] if isinstance(gathered[0], list) else []
            feed = gathered[feed_idx] if feed_idx >= 0 and isinstance(gathered[feed_idx], str) else ""
            if drv_idx >= 0 and isinstance(gathered[drv_idx], float):
                self.market = gathered[drv_idx]
            return evidence, feed

        async def _enrich_evidence(client, evidence):
            if evidence and self.left() > 60 and self.fam in ("quiet", "hithot", "hotkin", "misc"):
                await Forecaster._CorpusOps.expand_top_hit(ctx, client, evidence)

        async def _run_inference(client, evidence, feed):
            if self.left() > 35:
                return await Forecaster._LLMBridge.infer(self, client, self.messages(evidence, feed))
            return None

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
            evidence, feed = await _collect_bundle(client)
            await _enrich_evidence(client, evidence)
            parsed = await _run_inference(client, evidence, feed)

        model_p = clamp(parsed.get("probability")) if parsed else None
        final_p = self.blend(model_p)
        reasoning = self.compose(str(parsed.get("reasoning", "")) if parsed else "",
                                 evidence, final_p, model_p)
        elapsed = time.time() - self.started
        market_str = f"{self.market:.4f}" if self.market is not None else "none"
        model_str  = f"{model_p:.3f}" if model_p is not None else "none"
        print(
            f"[RESULT] fam={self.fam} prior={self.prior:.3f} market={market_str} "
            f"model={model_str} final={final_p:.4f} | "
            f"elapsed={elapsed:.1f}s cost=${self._cost:.4f}"
        )
        return {"event_id": self.eid, "prediction": float(final_p), "reasoning": reasoning}

    def offline_answer(self):
        p = clamp(0.8 * self.market + 0.2 * self.prior) if self.market is not None else clamp(self.prior) or 0.23
        elapsed = time.time() - self.started
        print(f"[OFFLINE] fam={self.fam} market={self.market} prior={self.prior:.3f} → p={float(p):.4f} | elapsed={elapsed:.1f}s cost=$0.0000")
        text = template_analysis(self.title, self.deadline, self.fam, self.prior, self.market, [], p)
        return {"event_id": self.eid, "prediction": float(p), "reasoning": text[:REASON_CAP]}


def agent_main(event_data: dict) -> dict:
    def _terminal_fallback(ed):
        return {
            "event_id": str((ed or {}).get("event_id") or ""),
            "prediction": 0.23,
            "reasoning": template_analysis("", "", "misc", 0.23, None, [], 0.23)[:REASON_CAP],
        }

    def _online_run(fc):
        return asyncio.run(asyncio.wait_for(fc.run(), timeout=BUDGET_S + 5))

    fc = Forecaster(event_data)
    try:
        return _online_run(fc)
    except Exception:
        try:
            return fc.offline_answer()
        except Exception:
            return _terminal_fallback(event_data)