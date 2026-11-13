#!/bin/sh
# The wrapper runs as uid 900 with only the capabilities the uid lifecycle needs (setuid/setgid to the pool uids, chown and kill to wipe
# them) and no_new_privs. The parser processes it forks run as pool uids with none.
exec setpriv --no-new-privs --reuid=900 --regid=900 --clear-groups \
  --bounding-set=-all,+setuid,+setgid,+chown,+kill --inh-caps=+setuid,+setgid,+chown,+kill --ambient-caps=+setuid,+setgid,+chown,+kill \
  python -m spider_hoarder_parser.wrapper
