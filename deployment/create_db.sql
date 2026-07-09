-- Create the database
CREATE DATABASE d4cg_smart_on_fhir;

-- Optional: create a dedicated user
CREATE USER d4cg_user WITH PASSWORD 'd4cgStrongPassword';

-- Grant ownership of the database
ALTER DATABASE d4cg_smart_on_fhir OWNER TO d4cg_user;

-- Allow the user to connect
GRANT ALL PRIVILEGES ON DATABASE d4cg_smart_on_fhir TO d4cg_user;