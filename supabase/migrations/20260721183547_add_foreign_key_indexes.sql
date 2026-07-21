set search_path = kol_search, public;

create index idx_automation_jobs_connection
    on automation_jobs(connection_id);
create index idx_automation_actions_opportunity
    on automation_actions(opportunity_id);
create index idx_automation_controls_connection
    on automation_channel_controls(connection_id);
create index idx_x_trend_tweets_tweet
    on x_trend_tweets(tweet_id);
