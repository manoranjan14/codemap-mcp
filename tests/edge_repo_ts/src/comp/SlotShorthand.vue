<template>
  <List :items="items">
    <template #default="{ row }">
      {{ formatRow(row) }}
    </template>
    <template #empty>
      {{ emptyMessage() }}
    </template>
  </List>
  <input :[dynamicProp]="computeValue()" @[dynamicEvent]="handleDynamic()" />
</template>

<script setup lang="ts">
function formatRow(row: unknown) {
  return String(row)
}

function emptyMessage() {
  return "nothing here"
}

function computeValue() {
  return 1
}

function handleDynamic() {
  return true
}
</script>
