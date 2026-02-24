#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────
// generate-skills.js — Dynamic skill generator from MCP server tools
// ─────────────────────────────────────────────────────────────
//
// Reads the MCP server Python file, extracts Tool() definitions,
// and generates SKILL.md files in skills/<tool-name>/SKILL.md.
//
// Works with any MCP server that uses the mcp.types.Tool pattern.
// Cross-platform: macOS, Linux, Windows.
//
// Usage:
//   node scripts/generate-skills.js                    # Generate from default mcp_server.py
//   node scripts/generate-skills.js --mcp-file path    # Custom MCP server file
//   node scripts/generate-skills.js --dry-run           # Preview without writing
//   node scripts/generate-skills.js --list              # List tools found
//
// ─────────────────────────────────────────────────────────────

const fs = require('fs');
const path = require('path');

const INSTALL_DIR = path.resolve(path.join(__dirname, '..'));
const SKILLS_DIR = path.join(INSTALL_DIR, 'skills');

// ── Parse CLI args ─────────────────────────────────────────

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const listOnly = args.includes('--list');
const mcpFileIdx = args.indexOf('--mcp-file');
const mcpFile = mcpFileIdx >= 0 && args[mcpFileIdx + 1]
  ? path.resolve(args[mcpFileIdx + 1])
  : path.join(INSTALL_DIR, 'mcp_server.py');

// ── Tool extraction ────────────────────────────────────────

/**
 * Extract Tool() definitions from a Python MCP server file.
 * Parses the list_tools() function to find Tool(name=..., description=..., inputSchema=...).
 *
 * Returns: [{ name, description, inputSchema }]
 */
function extractToolsFromPython(filePath) {
  const content = fs.readFileSync(filePath, 'utf8');

  // Find the list_tools function and extract the return block
  const listToolsMatch = content.match(
    /async\s+def\s+list_tools\s*\(\s*\)[\s\S]*?return\s*\[([\s\S]*?)\n    \]/
  );
  if (!listToolsMatch) {
    // Fallback: try a simpler pattern
    const simpleMatch = content.match(/return\s*\[\s*(Tool\([\s\S]*?)\s*\]/);
    if (!simpleMatch) {
      console.error('Could not find list_tools() return block');
      return [];
    }
    return parseToolBlock(simpleMatch[1]);
  }

  return parseToolBlock(listToolsMatch[1]);
}

/**
 * Parse individual Tool() calls from the extracted block.
 */
function parseToolBlock(block) {
  const tools = [];

  // Split by Tool( at the same indent level
  const toolBlocks = block.split(/\n\s+Tool\(/);

  for (let i = 0; i < toolBlocks.length; i++) {
    let tb = toolBlocks[i];
    if (i === 0) {
      // First block may start with Tool(
      const toolStart = tb.indexOf('Tool(');
      if (toolStart < 0) continue;
      tb = tb.slice(toolStart + 5);
    }

    // Extract name
    const nameMatch = tb.match(/name\s*=\s*"([^"]+)"/);
    if (!nameMatch) continue;
    const name = nameMatch[1];

    // Extract description — handle multi-line parenthesized strings
    let description = '';
    const descStartIdx = tb.indexOf('description');
    if (descStartIdx >= 0) {
      const afterDesc = tb.slice(descStartIdx);
      // Check if it's description=( ... ) or description="..."
      const eqMatch = afterDesc.match(/^description\s*=\s*/);
      if (eqMatch) {
        const afterEq = afterDesc.slice(eqMatch[0].length);
        if (afterEq.startsWith('(')) {
          // Multi-line: collect all quoted strings until matching close paren
          // Count parens to handle nested parens in description text
          let depth = 0;
          let idx = 0;
          let startFound = false;
          const descLines = afterEq.split('\n');
          const quotedParts = [];
          for (const line of descLines) {
            for (let ci = 0; ci < line.length; ci++) {
              if (line[ci] === '(') depth++;
              if (line[ci] === ')') depth--;
              if (depth === 0 && startFound) break;
              if (depth > 0) startFound = true;
            }
            // Extract all quoted strings from this line
            const strMatch = line.match(/"([^"]*)"/g);
            if (strMatch) {
              for (const s of strMatch) {
                quotedParts.push(s.slice(1, -1));
              }
            }
            if (depth === 0 && startFound) break;
          }
          description = quotedParts.join('');
        } else if (afterEq.startsWith('"')) {
          // Single-line
          const singleMatch = afterEq.match(/^"([^"]+)"/);
          if (singleMatch) description = singleMatch[1];
        }
      }
    }

    // Extract inputSchema properties using bracket-depth parsing
    const properties = {};
    const propsIdx = tb.indexOf('"properties"');
    if (propsIdx >= 0) {
      // Find the opening { after "properties":
      const colonAfterProps = tb.indexOf(':', propsIdx + 12);
      if (colonAfterProps >= 0) {
        const braceStart = tb.indexOf('{', colonAfterProps);
        if (braceStart >= 0) {
          // Find matching closing brace
          let depth = 0;
          let braceEnd = -1;
          for (let ci = braceStart; ci < tb.length; ci++) {
            if (tb[ci] === '{') depth++;
            if (tb[ci] === '}') depth--;
            if (depth === 0) { braceEnd = ci; break; }
          }
          if (braceEnd > braceStart) {
            const propsBlock = tb.slice(braceStart + 1, braceEnd);
            // Find each top-level property: "name": { ... }
            // Use a state machine to find property names at depth 0
            let pd = 0;
            let propStart = -1;
            let inPropValue = false;
            let currentPropName = '';
            for (let ci = 0; ci < propsBlock.length; ci++) {
              if (propsBlock[ci] === '{') {
                if (pd === 0 && currentPropName) {
                  propStart = ci;
                }
                pd++;
              }
              if (propsBlock[ci] === '}') {
                pd--;
                if (pd === 0 && propStart >= 0 && currentPropName) {
                  const propBody = propsBlock.slice(propStart + 1, ci);
                  const typeMatch = propBody.match(/"type"\s*:\s*"(\w+)"/);
                  const descPropMatch = propBody.match(/"description"\s*:\s*"([^"]+)"/);
                  properties[currentPropName] = {
                    type: typeMatch ? typeMatch[1] : 'string',
                    description: descPropMatch ? descPropMatch[1] : '',
                  };
                  currentPropName = '';
                  propStart = -1;
                }
              }
              // At depth 0, look for property names
              if (pd === 0) {
                const remaining = propsBlock.slice(ci);
                const nameM = remaining.match(/^"(\w+)"\s*:/);
                if (nameM) {
                  currentPropName = nameM[1];
                  ci += nameM[0].length - 1; // skip past the colon
                }
              }
            }
          }
        }
      }
    }

    const required = [];
    const requiredMatch = tb.match(/"required"\s*:\s*\[([^\]]*)\]/);
    if (requiredMatch) {
      const reqRegex = /"(\w+)"/g;
      let rm;
      while ((rm = reqRegex.exec(requiredMatch[1])) !== null) {
        required.push(rm[1]);
      }
    }

    tools.push({ name, description, properties, required });
  }

  return tools;
}

// ── Skill generation ───────────────────────────────────────

/**
 * Build argument hint from tool properties.
 * e.g. "<query> [--project NAME] [--limit N]"
 */
function buildArgumentHint(tool) {
  const parts = [];

  // Required params first (positional style)
  for (const r of tool.required) {
    if (tool.properties[r]) {
      parts.push(`<${r}>`);
    }
  }

  // Optional params
  for (const [k, v] of Object.entries(tool.properties)) {
    if (tool.required.includes(k)) continue;
    parts.push(`[--${k} ${v.type === 'integer' ? 'N' : v.type === 'array' ? 'LIST' : 'VALUE'}]`);
  }

  return parts.join(' ') || '';
}

/**
 * Generate a SKILL.md file for a tool.
 */
function generateSkillMd(tool) {
  const argHint = buildArgumentHint(tool);
  const toolNameHuman = tool.name.replace(/_/g, ' ');

  // Build parameter table
  let paramTable = '';
  if (Object.keys(tool.properties).length > 0) {
    paramTable = '\n## Parameters\n\n';
    paramTable += '| Param | Type | Required | Description |\n';
    paramTable += '|-------|------|----------|-------------|\n';
    for (const [k, v] of Object.entries(tool.properties)) {
      const req = tool.required.includes(k) ? 'Yes' : 'No';
      paramTable += `| \`${k}\` | ${v.type} | ${req} | ${v.description} |\n`;
    }
  }

  // Build execution section — shows how to call the MCP tool
  let execArgs = '';
  if (tool.required.length > 0) {
    execArgs = tool.required.map(r => `${r}="$ARGUMENTS"`).join(', ');
  }
  if (!execArgs) execArgs = '';

  return `---
name: ${tool.name}
description: "${tool.description.replace(/"/g, '\\"')}"
argument-hint: "${argHint}"
user-invocable: true
---

# ${toolNameHuman}

${tool.description}
${paramTable}
## Execution

When invoked with \`/${tool.name}\`, call the \`${tool.name}\` MCP tool (server: agent-memory)${execArgs ? ` with ${execArgs}` : ''}.

\`\`\`
${tool.name}(${execArgs || ''})
\`\`\`
`;
}

// ── Main ───────────────────────────────────────────────────

function main() {
  if (!fs.existsSync(mcpFile)) {
    console.error(`MCP server file not found: ${mcpFile}`);
    process.exit(1);
  }

  const tools = extractToolsFromPython(mcpFile);

  if (tools.length === 0) {
    console.error('No tools found in MCP server file');
    process.exit(1);
  }

  if (listOnly) {
    console.log(`Found ${tools.length} tools in ${path.basename(mcpFile)}:\n`);
    for (const t of tools) {
      const hint = buildArgumentHint(t);
      console.log(`  /${t.name}${hint ? ' ' + hint : ''}`);
      console.log(`    ${t.description}\n`);
    }
    return;
  }

  console.log(`Generating skills from ${path.basename(mcpFile)} (${tools.length} tools)...\n`);

  const generated = [];
  for (const tool of tools) {
    const skillDir = path.join(SKILLS_DIR, tool.name);
    const skillFile = path.join(skillDir, 'SKILL.md');
    const content = generateSkillMd(tool);

    if (dryRun) {
      console.log(`  [dry-run] Would write: skills/${tool.name}/SKILL.md`);
      generated.push({ name: tool.name, path: skillFile });
      continue;
    }

    if (!fs.existsSync(skillDir)) {
      fs.mkdirSync(skillDir, { recursive: true });
    }
    fs.writeFileSync(skillFile, content, 'utf8');
    console.log(`  Generated: skills/${tool.name}/SKILL.md`);
    generated.push({ name: tool.name, path: skillFile });
  }

  console.log(`\n${generated.length} skill(s) generated.`);
  return generated;
}

// Export for use by install.js
module.exports = { extractToolsFromPython, generateSkillMd, buildArgumentHint };

if (require.main === module) {
  main();
}
