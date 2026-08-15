"""Reading request bodies without letting a malformed one become a crash.

``await request.json()`` raises on anything that is not valid JSON, and an
unhandled raise on a route that a client can reach is a 500 where a 400 belongs.
Every route that takes a JSON body goes through here instead.
"""

from fastapi import HTTPException


async def read_json(request, required=True):
    """Parse a JSON object body, or raise a clean 400.

    Accepts a form post too, so a page that has to work without JavaScript —
    the login form — uses the same path as everything else.
    """
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/x-www-form-urlencoded" in content_type or \
            "multipart/form-data" in content_type:
        return dict(await request.form())

    try:
        body = await request.json()
    except Exception as exc:
        if required:
            raise HTTPException(
                status_code=400,
                detail="That request was not valid JSON.",
            ) from exc
        return {}

    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="That request should be a JSON object.",
        )
    return body
