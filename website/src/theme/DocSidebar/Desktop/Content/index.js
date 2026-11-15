// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import React, {useState} from 'react';
import OriginalContent from '@theme-original/DocSidebar/Desktop/Content';

function filterItems(items, query) {
  return items.flatMap((item) => {
    if (item.label?.toLowerCase().includes(query)) {
      return [{...item, collapsed: false}];
    }
    if (item.type === 'category') {
      const children = filterItems(item.items, query);
      return children.length ? [{...item, items: children, collapsed: false}] : [];
    }
    return [];
  });
}

export default function SidebarContent(props) {
  const [query, setQuery] = useState('');
  const search = query.trim().toLowerCase();
  const sidebar = search ? filterItems(props.sidebar, search) : props.sidebar;
  return (
    <>
      <div className="learn-sidebar-header">
        <p>Azure CycleCloud Multi-region plugin for AMLFS</p>
        <label htmlFor="filter-guides">Filter documentation</label>
        <input
          id="filter-guides"
          type="search"
          placeholder="Filter by title"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {search && sidebar.length === 0 && <p role="status">No matching guides.</p>}
      </div>
      <OriginalContent {...props} key={search} sidebar={sidebar} />
    </>
  );
}
