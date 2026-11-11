# One bridge per radio mesh

Co-locating MEDRE and another bridge on one radio mesh while both write the
same Matrix rooms is now documented as an unsupported deployment. MEDRE
relays every observed radio packet into the mapped rooms without a
peer-suppression surface for radio-origin echoes, so two bridges sharing a
medium and rooms feed each other's transmissions back until queues saturate.
The supported topology is a star, one bridge per mesh medium, joined only
through shared rooms; `docs/spec/deployment-constraints.md` states the
constraint, the failure mechanism, and the qualification evidence for the
star topology.
