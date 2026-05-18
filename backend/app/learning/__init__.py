"""Learning engine — closes the feedback loop.

Mines human corrections (the ``edits`` table) and validated sheets to
derive aliases, OCR confusion patterns and few-shot examples, then
materialises the approved ones into ``lexicons/learned_overlay.json``
which the OCR pipeline consumes on top of the static lexicons.

Runs after every 50 validated sheets (see :mod:`scheduler`).
"""
