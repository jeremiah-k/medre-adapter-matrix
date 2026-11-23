# Local service testing

Developer notes for the opt-in proofs that run the real adapter against a
locally provisioned homeserver. Normal adapter operation needs only an
existing Matrix account on your existing homeserver; the setup below is test
infrastructure, not user setup.

## Durable runtime live proof

The opt-in `test_matrix_durable_runtime_live.py` proof takes explicit local
homeserver accounts through environment variables. It runs the real MEDRE
runtime with durable storage and a fake destination across a restart, and
fails when native ingress fails. `MATRIX_ENCRYPTED=1` selects genuine Megolm
traffic; use fresh account/device stores for each independent run. The core
Synapse Docker starter can provision the isolated accounts.
