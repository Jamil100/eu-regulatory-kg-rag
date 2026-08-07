# infra (optional stretch)

Terraform for provisioning **Azure Database for PostgreSQL Flexible Server**
(with the `vector` extension) as a managed alternative to the local Docker
Postgres.

## Usage

```bash
cd infra
terraform init
terraform plan -var="admin_password=..."
terraform apply
```

Then point `POSTGRES_DSN` in `.env` at the provisioned server.
