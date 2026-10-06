"""Jev checks that a proposed Run says what the user asked for, before a grant starts it unasked.

The decider's objective and criteria are the only translation of the user's words: the implementer
and the reviewer both work from them. Any doubt here only downgrades a granted Run to the usual
Approve button; it can never approve anything.
"""

import json
import urllib.error
import urllib.request

from . import decider
from .db import connect


API = "https://api.typesafe.ai/v1/systemone"
CONVERSATION_MESSAGES = 10

# Each question passes when its probability is on the right side of its limit. These start strict:
# a needless button tap is cheap, a Run that builds the wrong thing is not. Scores are recorded with
# each proposal, so the limits can be tuned against which pull requests were merged.
QUESTIONS = {
    "covers_ask": {
        "type": "noul",
        "instructions": "The `proposed_run` objective and acceptance criteria include every change the user "
                        "asked for in `latest_user_message`, read together with the `conversation` it continues.",
        "criteria": {"true": "Nothing the user asked for is missing from the proposed run.",
                     "false": "At least one thing the user asked for is missing, weakened, or changed."},
    },
    "adds_scope": {
        "type": "noul",
        "instructions": "The `proposed_run` objective or acceptance criteria require work that the user did not "
                        "ask for and that is not a necessary part of doing what they asked.",
        "criteria": {"true": "The proposed run adds features, changes, or requirements beyond the request.",
                     "false": "Everything in the proposed run comes from the request or is needed to do it."},
    },
    "clear_ask": {
        "type": "noul",
        "instructions": "The user's request says clearly enough what should change that a developer could do it "
                        "without guessing what the user meant. Messages may contain speech transcription errors.",
        "criteria": {"true": "The intended change is clear.",
                     "false": "A developer would have to guess at the goal, the scope, or what done means."},
    },
    "wants_start": {
        "type": "noul",
        "instructions": "In `latest_user_message`, the user is asking for this work to be done now.",
        "criteria": {"true": "The user is requesting the work.",
                     "false": "The user is thinking aloud, asking a question, or discussing options."},
    },
}
PASS = {"covers_ask": (">=", 0.7), "adds_scope": ("<=", 0.3), "clear_ask": (">=", 0.6), "wants_start": (">=", 0.7)}
CONCERNS = {"covers_ask": "it may leave out part of what you asked",
            "adds_scope": "it may add work you didn't ask for",
            "clear_ask": "your request may be ambiguous",
            "wants_start": "you may not have asked to start yet"}


def load_config(path):
    with open(path) as source:
        config = json.load(source)
    if not isinstance(config, dict) or set(config) != {"api_key", "model", "timeout"} or \
       not isinstance(config["api_key"], str) or not config["api_key"] or \
       not isinstance(config["model"], str) or not config["model"] or \
       type(config["timeout"]) is not int or not 5 <= config["timeout"] <= 120:
        raise ValueError("fidelity configuration needs api_key, model, and timeout (5–120 seconds)")
    return config


def check(app, text, repo, objective, criteria):
    """Return {"scores": {question: probability} or None, "concerns": [text]}. No concerns means pass."""
    with connect(app.dsn) as conn:
        conversation = decider.history(conn)[-CONVERSATION_MESSAGES:]
    state = {"conversation": conversation, "latest_user_message": text,
             "proposed_run": {"repository": repo, "objective": objective, "acceptance_criteria": criteria}}
    body = {"model": app.fidelity["model"], "state": state, "questions": QUESTIONS}
    request = urllib.request.Request(app.fidelity_api, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + app.fidelity["api_key"]},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=app.fidelity["timeout"]) as response:
            answers = json.loads(response.read(1024 * 1024))["answers"]
        scores = {name: float(answers[name]["noul"]) for name in QUESTIONS}
    except urllib.error.HTTPError as exc:
        return {"scores": None, "concerns": [f"the request check was unavailable (TypeSafe returned {exc.code})"]}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"scores": None, "concerns": [f"the request check was unavailable ({type(exc).__name__})"]}
    concerns = [CONCERNS[name] for name, (op, limit) in PASS.items()
                if (scores[name] < limit if op == ">=" else scores[name] > limit)]
    return {"scores": scores, "concerns": concerns}
