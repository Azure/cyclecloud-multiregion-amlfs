// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import React from 'react';
import Link from '@docusaurus/Link';
import OriginalNavbar from '@theme-original/Navbar';

export default function Navbar(props) {
  return (
    <>
      <OriginalNavbar {...props} />
      <nav className="learn-product-nav" aria-label="Documentation sections">
        <Link to="/docs/overview">Overview</Link>
        <Link to="/docs/installation">Quick start</Link>
        <Link to="/docs/troubleshooting">Troubleshoot</Link>
      </nav>
    </>
  );
}
