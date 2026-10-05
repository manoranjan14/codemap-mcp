import { helperA, helperB as hb } from './utils/helpers';
import { Widget } from '@/widget';
import * as ns from 'lodash';

function outer() {
  helperA();
  const inner = () => {
    hb();
  };
  inner();
}

const arrowTop = () => {
  outer();
};

class Consumer {
  private w: Widget;
  method() {
    this.other();
    ns.debounce();
  }
  other = () => {
    arrowTop();
  }
}
