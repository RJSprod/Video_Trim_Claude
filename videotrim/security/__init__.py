"""Security layer: the filesystem boundary, authentication, and access policy.

Nothing in this package imports Qt or Gradio. The desktop app and the WebUI are
both clients of it, so the host-protection rules cannot differ between them.
"""
