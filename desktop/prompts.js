// Agent Pool — prompt builders for the desktop pane.
//
// Pure functions, no SDK import: each builder turns form state into the exact
// prompt text the pane submits to the bound agent session. Every prompt
// names the gateway profile, carries the literal slash command or script
// invocation with the form's fields as args, and asks the agent to return
// the plugin's reply verbatim. The pane never free-hands an operation.

function replySuffix() {
  return 'Return the plugin\'s reply verbatim.'
}

export function addRepoOwnerPrompt({ gateway, repoId, path, version }) {
  const versionFlag = version ? ` --version ${version}` : ''
  return `On the ${gateway} gateway, register this repo in the pool and ingest it as a new version: run /add-repo ${repoId} --path ${path}${versionFlag}. If the user did not supply a version and the pool already has accepted versions, first ask the maintainer gateway /fetch repo ${repoId} for accepted_version and use the next one. ${replySuffix()}`
}

export function addRepoLinkPrompt({ gateway, repoId, path, version, maintainerGateway }) {
  const versionFlag = version ? ` --version ${version}` : ''
  const target = maintainerGateway || 'the designated pool maintainer'
  return `On the ${gateway} gateway, link this repo into a designated pool (two steps, one tracked operation): (1) run scripts/contributor-add.py ${path} --repo ${repoId}${versionFlag} and capture the printed manifest_json and transfer_ticket; (2) send /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo ${repoId} to ${target}'s bot. Do not use remote URLs — local paths only. ${replySuffix()}`
}

export function fetchRepoListPrompt({ gateway }) {
  return `On the ${gateway} gateway, list the pools and repos: run /fetch repo list. ${replySuffix()}`
}

export function fetchTaskListPrompt({ gateway, repoId }) {
  const filter = repoId ? ` --repo ${repoId}` : ''
  return `On the ${gateway} gateway, bootstrap the catalog and list tasks: run /fetch repo list, then run /fetch task list${filter}. Reply with the task table. ${replySuffix()}`
}

export function claimPrompt({ gateway, ticketId, maxTasks }) {
  if (maxTasks && !ticketId) {
    return `On the ${gateway} gateway, auto-assign work: run /claim --any-project --max-tasks ${maxTasks}. Save the reply's worktree_path, branch (pool/<ticket_id>), base_ref and budget into the reply text so the pane can record them. ${replySuffix()}`
  }
  return `On the ${gateway} gateway, claim this ticket: run /claim ${ticketId}. Save the reply's worktree_path, branch (pool/${ticketId}), base_ref and budget into the reply text so the pane can record them. ${replySuffix()}`
}

export function pullPrompt({ gateway, repoId, ticketId, replyFile, dest }) {
  const ticket = ticketId || repoId
  const replyRef = replyFile ? ` --reply-file ${replyFile}` : ' --reply-file <saved claim reply json>'
  const destRef = dest || '<destination dir>'
  return `On the ${gateway} gateway, fetch the accepted bytes for ticket ${ticket} into a worktree: run scripts/contributor-claim.py ${ticket}${replyRef} --dest ${destRef}. Show the resulting worktree_path and branch. ${replySuffix()}`
}

export function pushGrantPrompt({ gateway, repoId, ticketId }) {
  return `On the ${gateway} gateway, request a push grant: run /push repo ${repoId} --ticket ${ticketId}. Return the grant token verbatim. ${replySuffix()}`
}

export function pushPackagePrompt({ gateway, worktreePath, repoId, poolId, version }) {
  return `On the ${gateway} gateway, package the completed work: run scripts/contributor-push.py ${worktreePath} --repo ${repoId} --pool ${poolId} --version ${version}. Take next_version from the grant reply or re-ask /fetch repo ${repoId} for accepted_version; never trust enroll-time chain_tips. ${replySuffix()}`
}

export function pushDeliverPrompt({ gateway, repoId, version, ticketJson, transferTicket, grantToken }) {
  const tokenFlag = grantToken ? ` --token ${grantToken}` : ' --token <grant_token>'
  return `On the ${gateway} gateway, deliver the packaged repo ${repoId} version ${version}: run /push repo --deliver ${transferTicket}${tokenFlag} --manifest '${ticketJson}'. Note: a redelivery is legal — the grant is not consumed by deliver — so offer "Retry deliver" on transport failure. ${replySuffix()}`
}

export function approvePrompt({ gateway, repoId, version, ticketId, deny }) {
  const denyFlag = deny ? ' --deny' : ''
  const target = ticketId
    ? `--ticket ${ticketId}`
    : `--repo ${repoId}${version ? ` --version ${version}` : ''}`
  return `On the ${gateway} gateway, OWNER ONLY: run scripts/pool-approve.py ${target}${denyFlag}. If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry. ${replySuffix()}`
}

export function mergePrompt({ gateway, repoId, ticketId, checkoutPath }) {
  const target = ticketId ? `ticket ${ticketId}` : `repo ${repoId}`
  const dest = checkoutPath || `work/integrate/${repoId}/`
  return `On the ${gateway} gateway, merge the approved bundle for ${target}: the plugin never merges — run the merge yourself. git fetch <approved bundle> and git merge it into the canonical checkout at ${dest}. Where the maintainer flow calls the merge_approved service path, do it through the plugin's documented flow, never a side import. If the audit is not clean or there is no accepted bundle, stop and report it verbatim. ${replySuffix()}`
}
