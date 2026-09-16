# ACP Setup

Robo can be used in text editors and IDEs that support [Agent Client Protocol](https://agentclientprotocol.com/overview/clients). Robo includes the `robo-acp` tool; `vibe-acp` remains available as a compatibility alias.
Once you have set up Robo with the API keys, you are ready to use `robo-acp` in your editor. Below are the setup instructions for some editors that support ACP.

## Zed

Configure Robo as a local ACP agent in Zed as follows:

1. Go to `~/.config/zed/settings.json` and, under the `agent_servers` JSON object, add the following key-value pair to invoke the `robo-acp` command. Here is the snippet:

```json
{
   "agent_servers": {
      "Robo": {
         "type": "custom",
         "command": "robo-acp",
         "args": [],
         "env": {}
      }
   }
}
```

2. In the `Agent Panel` view, select the `Robo` agent and start the conversation.

## JetBrains IDEs

For using Robo in JetBrains IDEs, you'll need to have the [Jetbrains AI Assistant extension](https://plugins.jetbrains.com/plugin/22282-jetbrains-ai-assistant) installed

### Version 2025.3 or later

1. Open settings, then go to `Tools > AI Assistant > Agents` and add Robo as a local ACP agent.

2. Open AI Assistant. You should be able to select Robo from the agent selector (if you're not authenticated yet, you will be prompted to do so).

### Legacy method

1. Add the following snippet to your JetBrains IDE acp.json ([documentation](https://www.jetbrains.com/help/ai-assistant/acp.html)):

```json
{
  "agent_servers": {
    "Robo": {
      "command": "robo-acp",
    }
  }
}
```

1. In the AI Chat agent selector, select the new Robo agent and start the conversation.

## Neovim (using avante.nvim)

Add Robo in the acp_providers section of your configuration

```lua
{
  acp_providers = {
    ["robo"] = {
      command = "robo-acp",
      env = {
         MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY"), -- necessary if you set up the Mistral provider manually
      },
    }
  }
}
```
