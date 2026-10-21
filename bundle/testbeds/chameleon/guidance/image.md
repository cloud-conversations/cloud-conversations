Images are named CC-<Distro><Version>. A build for a particular architecture or
accelerator carries a suffix, as in CC-Ubuntu24.04-ARM64. Choose the most specific
image whose suffix suits the node type or flavor you chose, and fall back to the base
image when none suits it. A flavor whose GPU is a mig_slice needs the image with VGPU
in its suffix. Name it with image_name, and give os_release as well so the rubric can
verify what booted.
