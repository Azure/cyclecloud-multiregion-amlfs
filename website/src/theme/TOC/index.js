// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import React from 'react';
import OriginalTOC from '@theme-original/TOC';

export default function TableOfContents(props) {
  return (
    <div className="learn-article-toc">
      <h2>In this article</h2>
      <OriginalTOC {...props} />
    </div>
  );
}
