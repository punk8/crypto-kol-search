set search_path = kol_search, public;

create index idx_automation_actions_agent_config_version
    on automation_actions(agent_config_version_id);
create index idx_automation_agent_commands_agent
    on automation_agent_commands(agent_id);
create index idx_automation_agents_current_config
    on automation_agents(current_config_version_id);
create index idx_automation_opportunities_agent_config_version
    on automation_opportunities(agent_config_version_id);
create index idx_automation_opportunities_agent
    on automation_opportunities(agent_id);
