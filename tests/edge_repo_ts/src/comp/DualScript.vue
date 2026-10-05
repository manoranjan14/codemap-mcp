<template>
  <div />
</template>

<script lang="ts">
// A .vue file may legally have BOTH a plain options/exports block and a
// setup block. Each block's top level is module scope, so top-level calls
// in EITHER must be recorded - not just the last one parsed.
import { helperA } from "../utils/helpers"
const plainBlockValue = helperA()
export default { name: "DualScript" }
</script>

<script setup lang="ts">
import { helperB } from "../utils/helpers"
const setupBlockValue = helperB()
</script>
