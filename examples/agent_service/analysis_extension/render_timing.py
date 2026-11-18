"""Page checks finish inside their enclosing transport deadline (seconds)."""

RENDER_SECONDS = 120
PAGE_STEP_SECONDS = 30
RENDER_HTTP_SECONDS = RENDER_SECONDS + 10
RENDER_REQUEST_SECONDS = RENDER_HTTP_SECONDS + 20
