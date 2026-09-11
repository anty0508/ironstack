# Running the relay as a stable, always-on service

The relay is a single Python 3 script with no dependencies, but run by hand
(`python3 relay_server.py`) it dies for mundane reasons and stays dead:

- the SSH session that started it closes,
- the box reboots,
- the process is OOM-killed or hits an unhandled error,
- it runs out of file descriptors under load.

`ironstack-relay.service` fixes the operational half of that: **systemd keeps it
running and restarts it on any exit**, with a raised file-descriptor limit and a
memory cap. The in-process hardening in `relay_server.py` fixes the rest (a
transient `accept()` error or a dead parked connection no longer takes it down).

## Install (run once on the relay box, as a sudo-capable user)

```bash
# 1. Get the code onto the box (adjust path/user to taste; the unit assumes
#    /home/anty/ironstack and User=anty).
git clone https://github.com/anty0508/ironstack.git /home/anty/ironstack
# ...or: git -C /home/anty/ironstack pull

# 2. Install the unit and start it.
sudo cp /home/anty/ironstack/deploy/ironstack-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ironstack-relay

# 3. Open the port (firewall + cloud security group).
sudo ufw allow 48920/tcp      # if using ufw; otherwise your firewall of choice
```

## Operate

```bash
systemctl status ironstack-relay          # is it up? how long? restart count?
journalctl -u ironstack-relay -f          # live logs ([relay] lines)
journalctl -u ironstack-relay --since "1 hour ago"
sudo systemctl restart ironstack-relay    # after pulling new code
```

## Confirming *why* it was dying (on the box)

```bash
# Was it OOM-killed?
journalctl -k | grep -i -E "killed process|out of memory"
# Did it hit the fd ceiling? (look for "accept failed" / EMFILE in the logs)
journalctl -u ironstack-relay | grep -i -E "accept failed|too many open files|capacity"
# How many fds is it holding right now?
ls -1 /proc/$(pgrep -f relay_server.py)/fd | wc -l
```

The hardened server logs `accept failed: ...` and `at capacity, dropping ...`
instead of exiting, so these now show up as log lines rather than a dead process.
