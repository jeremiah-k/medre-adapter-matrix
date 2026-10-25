# Preserve cross-platform context through Matrix

Safe metadata now carries a bounded source and relation context with native
references, sender claims and verification separate from visible prefix text.
Matrix ingress preserves valid context as remote relay hints without adopting
its claimed sender or relationships as local authority. Existing off/minimal
mode controls suppress the context; raw SDK data remains outside safe mode.
