"""DynamoDB stream handler: review newly closed trades automatically.

Only trades that closed in the last 3 days are reviewed, so a large history
import doesn't trigger thousands of AI calls. Older trades get a review on demand.
"""
import db
from util import now_ny, parse_iso
import review
from util import Unavailable


def _plain(image):
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer()
    return db.from_ddb({k: d.deserialize(v) for k, v in image.items()})


def handler(event, context):
    done = 0
    for rec in event.get("Records", []):
        img = rec.get("dynamodb", {}).get("NewImage")
        if not img:
            continue
        t = _plain(img)
        sub = t["PK"][5:]
        if t.get("status") == "open" and rec.get("eventName") == "INSERT":
            # a new position: live coach entry check (opt-in, only for fresh entries)
            try:
                from views import load_settings
                lc = load_settings(sub).get("liveCoach") or {}
                if lc.get("enabled") and lc.get("entry", True) and \
                        (now_ny() - parse_iso(t["openTs"])).total_seconds() < 86400:
                    import livecoach
                    livecoach.run(sub, "entry", t["id"])
            except Exception as e:
                print("entry coach skipped:", e)
            continue
        if t.get("status") != "closed" or not review.is_recent(t):
            continue
        if db.get(t["PK"], f"REVIEW#{t['id']}"):
            continue
        try:
            review.run(sub, t)
            done += 1
        except Unavailable as e:
            print("review skipped:", e)
    return {"reviewed": done}
