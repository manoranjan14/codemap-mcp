<template>
  <div>{{ vueTopTarget() }}</div>
</template>

<script setup lang="ts">
// <script setup> IS the component's setup function: every top-level call
// here really runs. This is the dominant Vue 3 style.
import { helperA } from "../utils/helpers"

function vueTopTarget() {
    return 1
}

const value = vueTopTarget()
const { picked } = helperA()
helperA()
</script>
