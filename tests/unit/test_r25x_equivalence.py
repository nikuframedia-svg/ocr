"""R250-R252 — Equivalência da variante OFF e isolamento do ContextVar.

O rollout da refundação matemática vive atrás de SCORING_VARIANT ("v30" |
"next"). Invariantes: (1) com a variante OFF (default), o motor é
byte-idêntico ao v30 — nenhum consumidor muda; (2) a variante é POR
CONTEXTO (thread/task): a sombra pode correr "next" em paralelo com a
produção em "v30" sem corrida (é o desenho do A/B de fábrica via
CROSS_SHADOW_VARIANT).
"""
from __future__ import annotations

import json
import threading

from app.pipeline.scoring_engine import (
    scoring_variant,
    set_scoring_variant,
    shadow_score,
)


_REFS = {
    "available": True,
    "of_to_entries": {
        "262593": [
            {
                "cliente": "TSO CATENAIRES", "ov": "2601149", "of": "262593",
                "designacao": "5100TME1 - CC4H1 5100T742 1/2",
                "esp": 12.0, "lbase": 737, "ltopo": 438, "comp": 8483,
            },
            {
                "cliente": "TSO CATENAIRES", "ov": "2601149", "of": "262593",
                "designacao": "5100TME2 - CC4H1 5100T743 1/2",
                "esp": 12.0, "lbase": 737, "ltopo": 438, "comp": 8483,
            },
        ],
    },
    "clientes_plan": frozenset({"TSO CATENAIRES"}),
    "lotes_sap_full": {},
}

_SHEET = {
    "template_name": "gasparini", "header": {}, "footer": {},
    "rows": [
        {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES",
         "modelo": "5100T742A", "qtd": "2"},
        {"of": "262593", "cliente": "TSO", "modelo": "5100T743B", "qtd": "1"},
    ],
}


def _score_json() -> str:
    scoring, *_ = shadow_score(json.loads(json.dumps(_SHEET)), None, _REFS)
    # timestamps/duração variam entre corridas — compara só a decisão
    scoring.pop("checked_at", None)
    scoring.pop("duration_ms", None)
    (scoring.get("summary") or {}).pop("duration_ms", None)
    return json.dumps(scoring, sort_keys=True, ensure_ascii=False)


class TestVariantOffEquivalence:
    def test_default_is_v30(self):
        assert scoring_variant() == "v30"

    def test_two_runs_byte_identical(self):
        # Determinismo com a variante OFF (paridade base para o rollout).
        assert _score_json() == _score_json()


class TestContextVarIsolation:
    def test_shadow_thread_does_not_leak_variant(self):
        # Produção (main thread, v30) e sombra ("next") em simultâneo:
        # a variante da sombra nunca vaza — e os lru_caches partilhados
        # não podem misturar resultados dependentes da variante.
        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def shadow() -> None:
            set_scoring_variant("next")
            barrier.wait()
            scoring, *_ = shadow_score(
                json.loads(json.dumps(_SHEET)), None, _REFS)
            results["shadow_variant"] = scoring_variant()
            results["shadow_of"] = (
                scoring["rows"][0]["fields"]["of"]["value"])

        t = threading.Thread(target=shadow)
        t.start()
        barrier.wait()
        scoring, *_ = shadow_score(json.loads(json.dumps(_SHEET)), None, _REFS)
        t.join()
        assert scoring_variant() == "v30"
        assert results["shadow_variant"] == "next"
        # Ambos decidem a mesma OF neste fixture (a variante muda a escala
        # de bits, não o winner aqui) — e a produção continua v30.
        assert scoring["rows"][0]["fields"]["of"]["value"] == results["shadow_of"]
