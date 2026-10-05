<script>
export default {
  name: "Legacy",
  methods: {
    greet() {
      return "hi"
    }
  }
}
</script>

<template>
  <div>{{ greet() }}</div>
</template>
