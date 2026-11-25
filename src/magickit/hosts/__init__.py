"""Work on a service host, asked for from somewhere that has no shell on it.

Design: spirrow-magickit ``T-host-task-request-with-human-approval``
(msg-1750 .. msg-1777; rulings msg-1770). This package is step 1 of that
design: a two-level registry (host -> targets) and ``host_probe``, the
typed, unapproved investigation door. Config changes (``host_task_request``,
the validator, approval and the apply runner) are later steps and are not
here.

What makes the unapproved door acceptable is that "read only" is a property
of the code, not of a judgement about a request (R-1):

- A probe is a name from a fixed catalogue (:mod:`.probes`). There is no
  free-text probe and no agent behind this door.
- Every argument names something in :mod:`.registry` (a host, a target, a
  registered file) or is a bounded integer. There is no path, unit name,
  regex or command fragment a caller can pass.
- Each probe builds a fixed argv (or reads ``/proc`` / a registered file
  directly) and runs it without a shell.
- What comes back is *rebuilt* from validated values -- bounded integers,
  closed enums, registry identifiers, fixed-format ids -- never passed
  through. Log message bodies are not returned at all (ruling 2, msg-1770);
  :mod:`.secretscan` is a last check on the rebuilt output, not the
  boundary.

The host key is a logical name (``services`` = ``{{HOST_SERVICES}}`` in
platform:infra-registry), not the machine's hostname: probe output lands in
an agent's context, and from there in chatroom posts and PR bodies.
"""
