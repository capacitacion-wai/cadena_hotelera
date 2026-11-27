ALTER TABLE reservas ADD COLUMN motivo_cancelacion VARCHAR(500);
ALTER TABLE reservas ADD COLUMN cancelada_por VARCHAR(255);
ALTER TABLE reservas ADD COLUMN cancelada_en VARCHAR(40);
