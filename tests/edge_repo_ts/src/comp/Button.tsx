import React from 'react';

export function Button() {
  const handleClick = () => {
    doSomething();
  };
  return <button onClick={handleClick}>Click</button>;
}

function doSomething() {
  console.log('clicked');
}
