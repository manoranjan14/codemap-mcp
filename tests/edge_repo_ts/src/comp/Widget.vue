<template>
  <button @click="save">{{ label }}</button>
</template>

<script setup lang="ts">
import { helperA } from "../utils/helpers"

function validate(x: number) {
  return x > 0
}

function save() {
  if (validate(1)) {
    helperA()
  }
}
</script>

<style scoped>
button { color: red; }
</style>
