"""The remote command must survive shell quoting.

`_build_provider_shell_url` sends the command to the provider as a list of argv parts
(cmd0, cmd1, ...). It used to build that list with `command.split(" ")`, which ignores
quoting -- so any command carrying a quoted argument was silently shredded:

    sh -c "df -h / && echo ok"
      -> ['sh', '-c', '"df', '-h', '/', '&&', 'echo', 'ok"']

The remote shell then received `"df` as a single argv and died with
`Syntax error: Unterminated quoted string`. That is *every* non-trivial command --
anything wrapped in `sh -c '...'`, which is how you run more than one thing. It went
unnoticed because the E2E only ever runs `echo hello from lease-shell`, which has no
quotes to lose.
"""

import urllib.parse

import pytest

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import LeaseShellTransport


def _argv_from_url(url: str) -> list[str]:
    """Recover the cmdN argv the transport actually sent.

    keep_blank_values=True is load-bearing, not a nicety. parse_qs defaults it to
    False, which SILENTLY DROPS any `cmdN=` carrying an empty value -- exactly the
    thing several of these tests exist to detect. With the default, the
    "consecutive spaces emit no empty argv" test below would pass even if the
    transport did emit empty argv parts: the helper would simply not see them, and
    the test would be vacuous. It also makes a legitimately empty argument (`sh -c ""`)
    invisible.
    """
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=True)
    parts = [(int(k[3:]), v[0]) for k, v in qs.items() if k.startswith("cmd")]
    return [v for _, v in sorted(parts)]


def _transport():
    t = LeaseShellTransport(
        TransportConfig(dseq="123", api_key="k", deployment={}, service_name="runner")
    )
    t._provider_host_uri = "https://provider.example:8443"
    t._service = "runner"
    return t


def test_quoted_sh_c_argument_survives_as_one_argv():
    """The load-bearing case: `sh -c "<script>"` must arrive as exactly 3 argv."""
    t = _transport()
    script = "df -h / && dd if=/dev/zero of=/tmp/p bs=1M count=1 && echo IO_PROBE_OK"
    url = t._build_provider_shell_url(command=f'sh -c "{script}"')

    argv = _argv_from_url(url)
    assert argv == ["sh", "-c", script], f"the quoted script must arrive as ONE argv; got {argv!r}"


def test_single_quoted_argument_survives():
    t = _transport()
    url = t._build_provider_shell_url(command="sh -c 'echo one && echo two'")
    assert _argv_from_url(url) == ["sh", "-c", "echo one && echo two"]


def test_unquoted_command_is_unchanged():
    """Backwards compatibility: the simple case must behave exactly as before."""
    t = _transport()
    url = t._build_provider_shell_url(command="echo hello from lease-shell")
    assert _argv_from_url(url) == ["echo", "hello", "from", "lease-shell"]


def test_consecutive_spaces_no_longer_emit_empty_argv():
    """`"a  b".split(" ")` produced ['a', '', 'b'] -- an empty cmdN param."""
    t = _transport()
    url = t._build_provider_shell_url(command="echo  spaced")
    assert _argv_from_url(url) == ["echo", "spaced"]


def test_unbalanced_quotes_raise_a_clear_error():
    t = _transport()
    with pytest.raises(RuntimeError, match="unbalanced quotes"):
        t._build_provider_shell_url(command='sh -c "never closed')
