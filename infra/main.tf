# Optional stretch: Azure Database for PostgreSQL Flexible Server with pgvector.
# Fill in and validate before use. Kept minimal by design.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  type    = string
  default = "westeurope"
}

variable "admin_password" {
  type      = string
  sensitive = true
}

resource "azurerm_resource_group" "this" {
  name     = "rg-kgrag"
  location = var.location
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                   = "kgrag-pg"
  resource_group_name    = azurerm_resource_group.this.name
  location               = azurerm_resource_group.this.location
  version                = "16"
  administrator_login    = "kgrag"
  administrator_password = var.admin_password
  storage_mb             = 32768
  sku_name               = "B_Standard_B1ms"
}

# Enable the vector extension on the server.
resource "azurerm_postgresql_flexible_server_configuration" "vector" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = "VECTOR"
}
