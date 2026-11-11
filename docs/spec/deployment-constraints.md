# Deployment Constraints

## One bridge per radio mesh

A radio mesh medium must carry message traffic from exactly one bridge when
the bridge's Matrix rooms are shared with other bridges. Deploying MEDRE and
another bridge (such as MMRelay) against the same radio mesh while both write
the same rooms is an unsupported configuration: MEDRE 0.1.0 relays every
packet its transport adapters observe into the mapped rooms and provides no
peer-suppression surface for radio-origin echoes, so each bridge reintroduces
the other's transmissions into the rooms, the other bridge relays them back
to the radio, and the cycle repeats with accumulating prefixed bodies until
radio queues and Matrix ingress saturate.

The supported topology is a star: each bridge attached to its own radio mesh,
connected only through the shared Matrix rooms. In that topology each message
airs at most once per mesh and the echo cycle cannot form; a live
qualification with two MMRelay instances and one multi-adapter MEDRE instance
on three separate meshes sharing one room family confirmed single airing per
mesh with flat queue and mapping growth. MMRelay documents the matching
constraint for its own deployments; the restriction binds whichever bridges
share the medium.

Multiple adapters within one MEDRE instance can serve distinct media, such as
separate radio meshes and an LXMF destination. The one-bridge constraint still
applies to each medium: two Meshtastic adapters attached to the same mesh are
not exempt merely because they run in one instance. Adapter type and instance
boundaries do not establish separate radio media.
