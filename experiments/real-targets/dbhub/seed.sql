PRAGMA journal_mode=DELETE;
CREATE TABLE tickets (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
  body TEXT NOT NULL,
  canary TEXT NOT NULL DEFAULT ''
);
INSERT INTO tickets(id, status, body) VALUES
  (1, 'open', 'Initial support note.'),
  (2, 'open', 'Separate protected ticket.');
