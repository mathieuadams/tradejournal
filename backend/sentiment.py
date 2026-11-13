"""Earnings call transcript scoring: tone, uncertainty, and the direction of guidance.

Word lists are this app's own (finance-oriented roots, matched at the start of each word: "declin" matches decline,
declined, declining). Tone = (positive − negative) / (positive + negative), from −1 to +1. Calls are split into the
prepared remarks and the Q&A (the Q&A is less scripted and often more telling). Guidance: sentences that talk about the
outlook, classified raise / lower / reaffirm by their verbs; the score is (raise − lower) / guidance sentences.
Negations ("not", "no", "never" within the 3 words before) flip a word's sign."""
import re

POS = """achiev advanc advantag align ahead attract beat benefit best better boost breakthrough confident creat deliver
 demand disciplin durabl efficien enabl encourag enhanc exceed excel expan favorabl gain good great grew grow healthy
 improv increas innovat lead momentum opportun optimis optimiz outpac outperform positiv pleas profitab progress
 record recover resilien robust solid stabil strength strong succe superior surpass sustain tailwind thrill upside
 accelerat win won exceptional milestone traction""".split()
NEG = """adverse against bankrupt burden challeng closure concern constrain contract cut damag decelerat declin decreas
 deficien delay deteriorat difficult disappoint disrupt doubt downgrad downside downturn drop erod fail fall fell
 force headwind hurt impair inflat investigat lawsuit layoff litigat lose loss losing lost low miss negativ penalt
 pressur problem recession reduc restat restructur risk severan shortfall shrink slow soft stagnat strain struggl
 terminat tough unfavorabl unprofitab volatil weak worse worsen writedown write-down shortage softness""".split()
UNC = """almost ambigu anticipat appear approximat assum believ could depend doubt estimat exposure fluctuat
 hope indefinit likel may maybe might nearly occasional pending perhaps possib predict preliminar probab
 random reassess roughly seem sometime somewhat speculat suggest tentativ uncertain uncertaint unclear unknown
 unpredict unsettl variab vari""".split()
NEGATE = {"not", "no", "never", "without", "neither", "nor", "hardly", "isn't", "wasn't", "aren't", "don't", "didn't", "won't"}
GUIDE_TERMS = re.compile(r"\b(guidance|outlook|forecast|full[- ]year|fiscal (?:year|20\d\d)|next quarter|for the (?:year|quarter|second half|back half)|"
                         r"we (?:now )?expect|we anticipate|we are (?:now )?(?:expecting|targeting)|range of)\b", re.I)
RAISE = re.compile(r"\b(rais(?:e|ed|ing)|increas(?:e|ed|ing) (?:our|the) (?:guidance|outlook|forecast)|higher end|upper end|above (?:the|our) (?:prior|previous)|"
                   r"upward|lift(?:ed|ing)?|boost(?:ed|ing)?|beat and raise)\b", re.I)
LOWER = re.compile(r"\b(lower(?:ed|ing)? (?:our|the|its) (?:guidance|outlook|forecast|expectations)|reduc(?:e|ed|ing) (?:our|the) (?:guidance|outlook|forecast)|"
                   r"cut(?:ting)? (?:our|the) (?:guidance|outlook|forecast)|below (?:the|our) (?:prior|previous)|downward|lower end|withdr[ae]w)\b", re.I)
REAFFIRM = re.compile(r"\b(reaffirm(?:ed|ing)?|reiterat(?:e|ed|ing)|maintain(?:ed|ing)? (?:our|the) (?:guidance|outlook)|unchanged|consistent with (?:our|the) (?:prior|previous))\b", re.I)
QA_SPLIT = re.compile(r"(question[- ]and[- ]answer|q\s*&\s*a session|open (?:up )?the (?:call|line|floor) (?:for|to) questions|"
                      r"(?:first|our first) question (?:comes|is from)|we will now (?:begin|take) (?:the )?questions)", re.I)
WORD = re.compile(r"[a-z][a-z'\-]+")
SENT = re.compile(r"(?<=[.!?])\s+")


def _match(w, roots):
    return any(w.startswith(r) for r in roots)


def _counts(text):
    words = WORD.findall(text.lower())
    pos = neg = unc = 0
    for i, w in enumerate(words):
        p, n = _match(w, POS), _match(w, NEG)
        if p or n:
            flip = any(x in NEGATE for x in words[max(0, i - 3):i])
            if p and not n:
                pos, neg = (pos, neg + 1) if flip else (pos + 1, neg)
            elif n and not p:
                pos, neg = (pos + 1, neg) if flip else (pos, neg + 1)
        if _match(w, UNC):
            unc += 1
    return len(words), pos, neg, unc


def _tone(pos, neg):
    return (pos - neg) / (pos + neg) if pos + neg else None


def guidance(text):
    """(score, raise, lower, reaffirm, sentences): guidance sentences and their direction."""
    up = dn = keep = 0
    picked = []
    for s in SENT.split(text):
        if len(s) > 600 or not GUIDE_TERMS.search(s):
            continue
        r, l_, k = bool(RAISE.search(s)), bool(LOWER.search(s)), bool(REAFFIRM.search(s))
        if r or l_ or k:
            up += r
            dn += l_
            keep += k and not (r or l_)
            if len(picked) < 8:
                picked.append({"text": s.strip()[:400], "dir": "raise" if r and not l_ else "lower" if l_ and not r else "reaffirm" if k else "mixed"})
    n = up + dn + keep
    return ((up - dn) / n if n else None), up, dn, keep, picked


def score(content):
    """Scores of one call transcript."""
    text = content or ""
    m = QA_SPLIT.search(text)
    prep, qa = (text[:m.start()], text[m.start():]) if m and m.start() > len(text) * 0.15 else (text, "")
    n, p, ng, u = _counts(text)
    n1, p1, ng1, _ = _counts(prep)
    n2, p2, ng2, _ = _counts(qa) if qa else (0, 0, 0, 0)
    g, up, dn, keep, sents = guidance(text)
    gdir = None
    if up + dn + keep:
        gdir = "raise" if g is not None and g > 0.2 and up >= 2 else "lower" if g is not None and g < -0.2 and dn >= 1 else "maintain"
    return {"words": n, "tone": _tone(p, ng), "tonePrep": _tone(p1, ng1), "toneQA": _tone(p2, ng2) if qa else None,
            "neg": ng / n * 1000 if n else None, "pos": p / n * 1000 if n else None, "uncert": u / n * 1000 if n else None,
            "guide": g, "guideUp": up, "guideDown": dn, "guideKeep": keep, "guideDir": gdir, "guideSents": sents, "qaShare": len(qa) / len(text) if text else None}
