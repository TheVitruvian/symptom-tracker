import re

_EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_EMAIL_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9-]+$")


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_semantic_email(value: str) -> bool:
    email = normalize_email(value)
    if not email or len(email) > 254:
        return False
    if email.count("@") != 1:
        return False

    local, domain = email.split("@", 1)
    if not local or not domain:
        return False
    if len(local) > 64:
        return False
    if local[0] == "." or local[-1] == "." or ".." in local:
        return False
    if not _EMAIL_LOCAL_RE.fullmatch(local):
        return False

    if domain[-1] == "." or ".." in domain or "." not in domain:
        return False
    labels = domain.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label[0] == "-" or label[-1] == "-":
            return False
        if not _EMAIL_DOMAIN_LABEL_RE.fullmatch(label):
            return False

    if len(labels[-1]) < 2:
        return False
    return True
