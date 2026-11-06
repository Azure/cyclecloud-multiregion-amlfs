// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import React from 'react';
import OriginalAnchor from '@theme-original/MDXComponents/A';

export default function MarkdownAnchor(props) {
  const {href} = props;
  // Bundled downloads are files, not routes to normalize with trailing slashes.
  if (props['data-noBrokenLinkCheck'] && href?.includes('/assets/files/')) {
    return <OriginalAnchor {...props} href={`pathname://${href}`} />;
  }
  return <OriginalAnchor {...props} />;
}
