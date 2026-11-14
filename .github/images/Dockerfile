# Not built. Dependabot reads this file to propose new versions of the tool images that the
# workflows and scripts run (F-56 b): it does not read `*_IMAGE` variables in a workflow.
#
# A bump here alone fails `backend/tests/test_workflow_images.py`, on purpose. Copy the new
# reference to every place the test names, and check the new image before merging: for cosign, its
# Sigstore signature as docs/DEPLOYMENT.md "Tool images" says, because it signs and verifies releases.
FROM aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c AS trivy
FROM ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d AS cosign
USER 65532
