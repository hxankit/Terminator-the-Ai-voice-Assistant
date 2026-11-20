"""Intent routing: fast keyword scoring, falling back to an LLM classifier when ambiguous."""
import json, logging, re
from ..llm import provider
from .registry import AGENTS

log = logging.getLogger("router")


def keyword_scores(text: str) -> dict[str, int]:
    t = f" {text.lower()} "
    return {k: sum(1 for kw in a.keywords if kw in t) for k, a in AGENTS.items() if a.keywords}


async def route(text: str, spec=None, has_attachments: bool = False, prev_agent: str | None = None) -> tuple[str, str]:
    """Returns (agent_key, method). prev_agent: the agent already handling this conversation."""
    scores = keyword_scores(text)
    if has_attachments and re.search(r"\b(csv|xlsx?|spreadsheet|data)\b", text, re.I):
        scores["data"] = scores.get("data", 0) + 2
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top, second = ranked[0], ranked[1]
    if top[1] >= 2 and top[1] >= second[1] + 2:
        return top[0], "keywords"                       # a clear new topic switches specialists
    if prev_agent in AGENTS and prev_agent != "general":
        # follow-ups ("tell her", "do the same for X", "yes") keep the specialist already on the job;
        # a single stray keyword isn't enough to drop the conversation's context
        return prev_agent, "context"
    if spec is not None and len(text) > 15:
        menu = "\n".join(f"- {k}: {a.description}" for k, a in AGENTS.items())
        try:
            raw = await provider.complete(spec, [
                {"role": "system", "content": f"Classify the user's request to one agent.\n{menu}\n"
                                              'Reply ONLY with JSON: {"agent": "<key>"}'},
                {"role": "user", "content": text[:2000]}], timeout=20)
            m = re.search(r"\{.*?\}", raw, re.S)
            key = json.loads(m.group(0)).get("agent") if m else None
            if key in AGENTS:
                return key, "llm"
        except Exception as e:
            log.info("llm routing failed, using keywords: %s", e)
    if top[1] >= 1 and top[1] > second[1]:
        return top[0], "keywords"
    return "general", "default"
