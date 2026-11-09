"""GraphQL documents for the Shopify Admin API.

Kept apart from the adapter because they are data, not behaviour: every one of
these was validated against the live schema with ``validate_graphql_codeblocks``
before it went in, and they are the thing to re-validate when the API version
moves.
"""

from __future__ import annotations

LOCATIONS_QUERY = """
query connectorLocations {
  locations(first: 10) {
    nodes { id name isActive shipsInventory }
  }
}
"""

# Both identifiers are requested on purpose. ``Publication.name`` is deprecated
# in favour of ``Catalog.title``, but ``catalog`` comes back null on stores that
# have no catalog attached -- which includes a plain trial store, where the
# non-deprecated path identifies nothing at all. Preferring the new field and
# falling back to the old one works today and survives the removal.
PUBLICATIONS_QUERY = """
query connectorPublications {
  publications(first: 25) {
    nodes { id autoPublish catalog { title } name }
  }
}
"""

PRODUCT_SET_MUTATION = """
mutation connectorProductSet($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product {
      id
      handle
      status
      variants(first: 1) { nodes { id sku price } }
    }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_PUBLISH_MUTATION = """
mutation connectorPublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { availablePublicationsCount { count } }
    userErrors { field message }
  }
}
"""

VARIANT_BY_SKU_QUERY = """
query connectorVariantBySku($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes { id sku inventoryItem { id } }
  }
}
"""

INVENTORY_SET_MUTATION = """
mutation connectorInventorySet($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message }
  }
}
"""

FULFILLMENT_ORDERS_QUERY = """
query connectorFulfillmentOrders($id: ID!) {
  order(id: $id) {
    id
    name
    fulfillmentOrders(first: 10) {
      nodes {
        id
        status
        lineItems(first: 50) { nodes { id remainingQuantity } }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation connectorFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo(first: 10) { company number url } }
    userErrors { field message }
  }
}
"""
