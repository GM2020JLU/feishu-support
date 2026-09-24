ALTER TABLE global_control_panels ADD COLUMN control_channel TEXT NOT NULL DEFAULT 'telegram'
CHECK(control_channel IN ('telegram','gui'));
