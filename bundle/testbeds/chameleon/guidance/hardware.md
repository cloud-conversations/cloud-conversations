Prefer a VM when a flavor meets the request, a GPU flavor included: a VM may be
leased for far longer than a bare-metal node. Choose bare metal when no flavor meets
the request, or when it needs a whole machine or a named node type. Avoid a site the
catalog marks as retiring, unless the request names it or no other site has a flavor
that meets it.

A size the request names picks the flavor with that size in its name: "large" is
m1.large, never m1.xlarge.
