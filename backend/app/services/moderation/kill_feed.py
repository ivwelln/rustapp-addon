from app.models import CollectionJob

PAGE_LIMIT = 25


def update_coverage(
    job: CollectionJob, payload: dict, rows: list[dict], *, start: int, end: int
) -> None:
    limit = payload.get("limit", PAGE_LIMIT)
    if type(limit) is not int or not 1 <= limit <= PAGE_LIMIT:
        return
    total = payload.get("total", len(rows))
    if type(total) is not int or total < len(rows) or len(rows) > limit:
        return
    timestamps = [row.get("created_at") for row in rows]
    if any(type(at) is not int or not start <= at <= end for at in timestamps):
        return
    if timestamps != sorted(timestamps, reverse=True):
        return
    # Events tied at the oldest timestamp can spill onto the next page.
    page_start = (
        timestamps[-1] + 1 if timestamps and (len(rows) == limit or total > len(rows)) else start
    )
    if not timestamps and total:
        return
    if job.kill_feed_covered_through is not None and end <= job.kill_feed_covered_through:
        return
    if page_start > end:
        return
    if job.kill_feed_covered_from is not None and job.kill_feed_covered_through is not None:
        if page_start <= job.kill_feed_covered_through + 1:
            page_start = min(page_start, job.kill_feed_covered_from)
    job.kill_feed_covered_from = page_start
    job.kill_feed_covered_through = end
