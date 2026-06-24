# Sports Content Factory

## Agents
- tennis/  — ATP/WTA tennis agent (ACTIVE, running as tennis-agent.service)
- wnba/    — WNBA agent (IN DEVELOPMENT)

## Structure
- agents/  — one folder per sport, each agent is independent
- core/    — shared utilities (future)
- logs/    — centralized logs (future)
- dashboard/ — status dashboard (future)

## Notes
- Each agent is fully independent with its own .env and dependencies
- Original tennis agent still lives in /root/tennis-agent/ and runs as systemd service
