services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    env_file: [.env]
    volumes:
      - teleroute_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U \"${POSTGRES_USER}\" -d \"${POSTGRES_DB}\""]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 10s
    networks:
      network-lml:
        aliases:
          - postgres

  db_migrate:
    image: ${IMAGE_PATH}:${VERSION}
    restart: "no"
    env_file: [.env]
    depends_on:
      postgres:
        condition: service_healthy
    working_dir: /opt/teleroute
    command:
      - python
      - scripts/apply_postgres_schema.py
    networks:
      - network-lml

  app:
    image: ${IMAGE_PATH}:${VERSION}
    container_name: lml-${CI_PROJECT_NAME}
    restart: unless-stopped
    env_file: [.env]
    ports:
      - "8000:8000"
    volumes:
      - teleroute-logs:/app/logs
    depends_on:
      postgres:
        condition: service_healthy
      db_migrate:
        condition: service_completed_successfully
    working_dir: /opt/teleroute
    command:
      - python
      - scripts/run_production.py
    networks:
      network-lml:
        aliases:
          - ${CI_PROJECT_NAME}

volumes:
  teleroute_postgres_data:
  teleroute-logs:

networks:
  network-lml:
    external: true
    name: network-lml
