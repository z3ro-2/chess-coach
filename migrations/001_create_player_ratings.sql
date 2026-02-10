CREATE TABLE IF NOT EXISTS player_ratings (
  player_username TEXT NOT NULL,
  game_url TEXT PRIMARY KEY,
  end_time TIMESTAMPTZ NOT NULL,
  rating INTEGER,
  time_control TEXT,
  rated BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_player_ratings_username_end_time
ON player_ratings (player_username, end_time DESC);
