# Running the bot 24/7 on a Linux VM

Any small always-on Ubuntu/Debian machine works (cloud VM, Raspberry Pi). The bot needs very little:
one Python process, ~100 API requests per minute, a few MB of RAM.

**Run only ONE bot at a time** (VM *or* your PC, never both). Two bots share the account's
rate limit and would trade against the same positions.

## 1. Copy the project to the VM
From your PC (PowerShell), replacing `user@VM_IP`:
```
scp -r "$HOME\Documents\SIG prediction cup" user@VM_IP:/tmp/sig-bot
```
Do not copy `logs\`, `state\` or any `.env` file if you made one. The key goes in step 3 instead.

## 2. Set up user, files and Python (on the VM)
```
sudo adduser --disabled-password --gecos "" susq
sudo mv /tmp/sig-bot /home/susq/sig-bot && sudo chown -R susq:susq /home/susq/sig-bot
sudo apt update && sudo apt install -y python3 python3-venv
sudo -u susq bash -c 'cd ~/sig-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo chmod +x /home/susq/sig-bot/deploy/run_forever.sh
```

## 3. Put the API key on the VM (type it yourself; it is never stored in the project)
```
sudo bash -c 'umask 077; read -rsp "SUSQ_API_KEY: " k; echo; echo "SUSQ_API_KEY=$k" > /etc/susq-arb.env'
```

## 4. Check, then start
Dry run first (no orders):
```
sudo bash -c 'set -a; . /etc/susq-arb.env; set +a; cd /home/susq/sig-bot && .venv/bin/python execute.py --once; chown -R susq:susq logs state'
```
Before starting the VM bot, stop the bot on your PC (create `STOP` in the PC folder).
Then install and start the service (it also starts after every reboot):
```
sudo cp /home/susq/sig-bot/deploy/susq-arb.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now susq-arb
```

## Everyday use
| What | Command on the VM |
|---|---|
| Watch the bot | `tail -f /home/susq/sig-bot/logs/live-$(date +%Y%m%d).txt` |
| Stop (cancels its resting orders, no restart) | `sudo -u susq touch /home/susq/sig-bot/STOP` |
| Start again | `sudo -u susq rm /home/susq/sig-bot/STOP && sudo systemctl restart susq-arb` |
| Status | `systemctl status susq-arb` |

A halt (for example legs that could not be evened out) writes `STOP` with the reason inside;
read it with `cat /home/susq/sig-bot/STOP` before starting again.
