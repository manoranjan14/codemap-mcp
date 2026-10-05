const helpers = require("./utils/helpers");
const { helperA, helperB: renamedB } = require("./utils/helpers");
const lodash = require("lodash");

function useNamespace() {
  return helpers.helperA();
}

function useDestructured() {
  return helperA() + renamedB();
}

function useBarePackage() {
  return lodash.get({}, "x");
}
