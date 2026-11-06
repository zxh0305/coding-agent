"""Recognize leaked tool protocols without parsing or executing their contents."""
import re

_PROTOCOL = re.compile(r"(?m)^\s*<(?:[|｜]{1,2}DSML[|｜]{1,2}\s*(?:calls|invoke|parameter)\b|tool_calls?\b|function_calls\b)",re.I)

def invalid_reply(message):
    if message.get('tool_calls'):
        return True
    content=message.get('content') or ''
    if not isinstance(content,str):return True
    # Explaining a protocol inside a code fence remains a legitimate answer.
    prose=re.sub(r'```[^\n]*\n.*?```','',content,flags=re.S)
    return bool(_PROTOCOL.search(prose))
